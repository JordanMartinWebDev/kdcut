"""Beat-synced montages: a stretch of building or mining cut to the beat of a song, so the
block sounds land on the beat ("dum, dum, dum" = place, break, place).

  1. Hits: blocks breaking or being placed, found on the game audio track (your voice is on
     its own track, so it doesn't get in the way) by matching Minecraft's own block sounds
     (mcsounds), so eating, burps, pickups, damage and footsteps don't count. Menus skipped.
  2. Beats: the beat grid of an upbeat song from your library. Most Epidemic tracks carry a
     BPM tag, so only where the beats fall has to be found.
  3. Slices: one short piece of gameplay per beat (every other beat for fast songs), in the
     order they happened, each starting just before its hit so the hit lands on the beat.
     Because it's all jump cuts, slow building works too: only the number of hits matters.
  4. Mix: the song comes up, and each slice's game sound plays only in a short window around
     its hit, so the hits snap on the beat instead of muddying it.

The montage is rendered as one clip with its own music; the episode's music bed goes quiet
under it. Needs numpy (bundled with the app).
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from kdcut import media

SR = 11025          # analysis sample rate
HOP = 128           # ~11.6 ms per onset frame
WIN = 1024
PRE = 0.10          # each slice starts this long before its hit (the swing before the sound)
HIT_WINDOW = 0.45   # game audio kept around each hit
MIN_HITS = 8        # fewer usable hits than this: fall back to a fast-forward


# --------------------------------------------------------------------------------------
# Audio analysis
# --------------------------------------------------------------------------------------

def decode(path: Path, start: float, seconds: float, stream: int | None = None):
    """Mono float32 samples at SR from `start` (seconds on the file's own timeline).
    Seeks roughly, then trims on the file's timestamps (-copyts): a plain -ss seek can land the
    audio ~30 ms off, which is plenty to miss a beat. render() cuts the same way."""
    import numpy as np
    coarse = max(start - 3.0, 0.0)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{coarse:.3f}", "-copyts", "-i", str(path)]
    if stream is not None:
        cmd += ["-map", f"0:a:{stream}"]
    cmd += ["-vn", "-af", f"atrim=start={start:.4f}:end={start + seconds:.4f},asetpts=PTS-STARTPTS",
            "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    proc = media.run_quiet(cmd, timeout=600, text=False)
    if proc.returncode != 0:
        raise media.MediaError((proc.stderr or b"").decode("utf-8", "replace").strip()[:300] or "decode failed")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def onset_envelope(x, low_only: bool = False):
    """Spectral flux: how much new sound starts in each frame. Returns (env, frames/second);
    frame i is at frame_time(i). With `low_only` only the low end (kick and bass, below
    ~250 Hz) counts: that's where the beat is, and hi-hats would pull it onto the off-beat.
    Game hits use the full range (block sounds are broadband)."""
    import numpy as np
    fps = SR / HOP
    if len(x) < WIN:
        return np.zeros(1, dtype=np.float32), fps
    n = 1 + (len(x) - WIN) // HOP
    win = np.hanning(WIN).astype(np.float32)
    bins = WIN // 2 + 1
    weight = np.ones(bins, dtype=np.float32)
    if low_only:
        weight[int(250 * WIN / SR) + 1:] = 0.0
    env = np.zeros(n, dtype=np.float32)
    prev = None
    for a in range(0, n, 2048):                  # in chunks: a 15-minute stretch stays small
        b = min(a + 2048, n)
        idx = np.arange(a, b)[:, None] * HOP + np.arange(WIN)[None, :]
        mag = np.log1p(10 * np.abs(np.fft.rfft(x[idx] * win, axis=1))).astype(np.float32)
        stack = mag if prev is None else np.vstack([prev[None, :], mag])
        flux = (np.maximum(np.diff(stack, axis=0), 0) * weight).sum(axis=1)
        env[b - len(flux):b] = flux             # the very first frame has nothing before it
        prev = mag[-1]
    m = float(env.max())
    return (env / m if m > 0 else env), fps


def frame_time(i: float) -> float:
    """When a sound that peaks the flux at frame i actually starts (measured on clicks: the
    flux peaks while the onset sits about three quarters into the window)."""
    return (i * HOP + 0.75 * WIN) / SR


def _bpm(tag: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", tag or "")
    if not m:
        return None
    v = float(m.group())
    return v if 50 <= v <= 220 else None


def beat_grid(env, fps: float, bpm: float | None = None) -> tuple[float, float]:
    """(seconds per beat, first beat time) that best fit the onset envelope. A BPM tag is
    trusted to within 3%; without one the tempo comes from the envelope's autocorrelation."""
    import numpy as np
    n = len(env)
    if bpm:
        candidates = [60.0 / (bpm * f) for f in np.linspace(0.97, 1.03, 25)]
    else:
        e = env - env.mean()
        ac = np.correlate(e, e, mode="full")[n - 1:]
        lags = np.arange(int(fps * 60 / 180), int(fps * 60 / 70) + 1)
        lags = lags[lags < n]
        if not len(lags):
            return 0.5, 0.0
        best = lags[int(np.argmax(ac[lags]))]
        rough = 60.0 / (best / fps)
        # Songs sit on whole (or half) BPMs: try those around the rough estimate.
        candidates = [60.0 / b for b in np.arange(round(rough * 2) / 2 - 3, round(rough * 2) / 2 + 3.01, 0.5)]
    best = (-1.0, 0.5, 0.0)
    for period in candidates:
        p = period * fps
        k = np.arange(0, max(int((n - 1) / p), 1))
        for phase in range(0, max(int(p), 1)):
            idx = (phase + k * p).astype(int)
            idx = idx[idx < n]
            if not len(idx):
                continue
            score = float(env[idx].mean())
            if score > best[0]:
                best = (score, period, frame_time(phase) % period)
    return best[1], best[2]


def find_hits(env, fps: float, start: float, skip: list[tuple[float, float]] = (),
              min_gap: float = 0.12, loose: bool = False) -> list[tuple[float, float]]:
    """(time, strength) of sharp sounds: local peaks of the onset envelope that stand well
    above the stretch's usual level. Times inside `skip` spans (menus) are left out."""
    import numpy as np
    if len(env) < 3:
        return []
    med = float(np.median(env))
    mad = float(np.median(np.abs(env - med))) or 1e-6
    peaks = np.sort(env)[-max(len(env) // 200, 1):]          # the loudest 0.5% of frames
    if loose:       # something else (the block-sound matcher) decides which hits count
        thr = max(med + 3 * mad, 0.12 * float(np.median(peaks)), 0.03)
    else:
        thr = max(med + 6 * mad, 0.35 * float(np.median(peaks)), 0.08)
    w = max(int(min_gap * fps), 1)
    out = []
    for i in np.nonzero(env > thr)[0]:
        a, b = max(i - w, 0), min(i + w + 1, len(env))
        if env[i] < env[a:b].max():
            continue
        t = start + frame_time(i)
        if any(s <= t <= e for s, e in skip):
            continue
        if out and t - out[-1][0] < min_gap:
            continue
        out.append((round(t, 3), float(env[i])))
    return out


# --------------------------------------------------------------------------------------
# Laying out the slices
# --------------------------------------------------------------------------------------

def focus(hits: list[tuple[float, float]], n: int, slot: float, room: float = 1.3) -> list[tuple[float, float]]:
    """The busiest run of block sounds that can fill `n` slices of `slot` seconds: of all
    stretches long enough (n * slot * room seconds), the one with the most block sounds. A
    lone block sound far from the real work (breaking a log between bites of food) stays out
    of the montage, and the slices show one continuous bit of building or mining."""
    span = n * slot * room
    best, best_count = None, 0
    j = 0
    for i in range(len(hits)):
        while j < len(hits) and hits[j][0] - hits[i][0] < span:
            j += 1
        if j >= len(hits):          # this stretch runs off the end: measure it from the end instead
            k = i
            while k > 0 and hits[-1][0] - hits[k - 1][0] < span:
                k -= 1
            count = len(hits) - k
            if count > best_count:
                best, best_count = (k, len(hits)), count
            break
        if j - i > best_count:
            best, best_count = (i, j + 1), j - i
    return hits[best[0]:best[1]] if best else hits


def choose(hits: list[tuple[float, float]], durations: list[float]) -> list[float] | None:
    """One hit per slice, in order, each far enough after the last that slices never repeat
    footage. Walks through the hits taking every one that fits, then keeps the strongest one
    from each stretch of those, so the cuts cover the run evenly and land on clear sounds."""
    n = len(durations)
    slot = max(durations) if durations else 0.0
    fits: list[tuple[float, float]] = []
    for t, strength in hits:
        if not fits or t - fits[-1][0] >= slot:
            fits.append((t, strength))
        elif strength > fits[-1][1] and (len(fits) < 2 or t - fits[-2][0] >= slot):
            fits[-1] = (t, strength)             # a clearer sound just after: use that one
    if len(fits) < n:
        return None
    picked: list[float] = []
    for k in range(n):
        lo, hi = k * len(fits) // n, max((k + 1) * len(fits) // n, k * len(fits) // n + 1)
        bucket = [f for f in fits[lo:hi] if not picked or f[0] - picked[-1] >= slot]
        if not bucket:
            return None
        picked.append(max(bucket, key=lambda f: f[1])[0])
    return picked


@dataclass
class Plan:
    song: Path
    song_name: str
    song_start: float               # seconds into the song where the montage starts
    beats: list[float]              # beat times on the montage clip (seconds); first one at PRE
    hits: list[float]               # source time of each slice's hit (seconds in the recording)
    bpm: float
    step: int                       # cut every `step` beats

    @property
    def seconds(self) -> float:
        return self.beats[-1] - PRE


def lay_out(hits: list[tuple[float, float]], song: Path, song_name: str, period: float, phase: float,
            song_len: float, target: float = 15.0, skip_intro: float = 12.0) -> Plan | None:
    """Fit a montage of about `target` seconds: cut on every beat for slow songs, every other
    beat (or every fourth) for fast ones, since slices shorter than ~0.4 s start to strobe.
    Starts on a beat past the song's intro when the song is long enough."""
    step = 1
    while period * step < 0.4:
        step *= 2
    slot = period * step
    beat_times = [phase + k * period for k in range(int((song_len - phase) / period) + 1)]
    for want in range(max(int(round(target / slot)), MIN_HITS), MIN_HITS - 1, -1):
        length = slot * want
        fits = [b for b in beat_times if b - PRE >= 0 and b + length + 0.3 <= song_len]
        if not fits:
            continue
        first = next((b for b in fits if b >= skip_intro), fits[-1])
        picked = None
        for room in (1.3, 1.7, 2.2, 3.0):        # the busiest run first, widened until it fits
            picked = choose(focus(hits, want, slot, room), [slot] * want)
            if picked:
                break
        picked = picked or choose(hits, [slot] * want)
        if picked:
            beats = [PRE + slot * k for k in range(want + 1)]
            return Plan(song, song_name, first - PRE, beats, picked, round(60.0 / period, 1), step)
    return None


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------

def render(plan: Plan, rec: Path, game_stream: int | None, dest: Path, fps, width: int, height: int,
           music_db: float, hit_gains: list[float] | None = None, log=None) -> tuple[Path, int]:
    """One clip: the slices back to back, the song, and each slice's hit. Returns (file, frames)."""
    fps = Fraction(fps)
    cuts = [int(round((b - PRE) * fps)) for b in plan.beats]
    frames = cuts[-1] - cuts[0]
    total = frames / float(fps)
    sig = (f"{rec}|{plan.song}|{plan.song_start:.3f}|{plan.hits}|{cuts}|{game_stream}|{music_db}|{hit_gains}|"
           f"{fps}|{width}x{height}|v7")
    dest = dest.with_name(f"{dest.stem}_{hashlib.sha1(sig.encode()).hexdigest()[:10]}.mp4")
    if dest.is_file():
        return dest, frames
    cmd = ["ffmpeg", "-y", "-v", "error"]
    filters, vlabels, alabels = [], [], []
    n = len(plan.hits)
    for k, t in enumerate(plan.hits):
        f = cuts[k + 1] - cuts[k]
        t0 = max(t - PRE, 0.0)
        # Rough seek, then exact trims on the file's own timestamps (same as decode()).
        cmd += ["-ss", f"{max(t0 - 3.0, 0):.3f}", "-copyts", "-i", str(rec)]
        filters.append(f"[{k}:v]trim=start={t0:.4f},setpts=PTS-STARTPTS,fps={fps},"
                       f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,trim=end_frame={f},"
                       f"setpts=PTS-STARTPTS[v{k}]")
        vlabels.append(f"[v{k}]")
        if game_stream is not None:
            delay = int(round(cuts[k] / float(fps) * 1000))
            g = (hit_gains[k] if hit_gains else 0.0)
            filters.append(f"[{k}:a:{game_stream}]atrim=start={t0:.4f}:end={t0 + PRE + HIT_WINDOW:.4f},"
                           f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                           f"volume={g:.1f}dB,"
                           f"afade=t=in:d=0.02,afade=t=out:st={PRE + HIT_WINDOW - 0.15:.3f}:d=0.15,"
                           f"adelay={delay}|{delay}[h{k}]")
            alabels.append(f"[h{k}]")
    cmd += ["-ss", f"{max(plan.song_start - 3.0, 0):.3f}", "-copyts", "-i", str(plan.song)]
    filters.append(f"[{n}:a]atrim=start={plan.song_start:.4f}:end={plan.song_start + total + 0.1:.4f},"
                   f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={music_db:.1f}dB,"
                   f"afade=t=in:d=0.08,afade=t=out:st={max(total - 0.35, 0):.3f}:d=0.35[mus]")
    filters.append("".join(vlabels) + f"concat=n={n}:v=1:a=0,format=yuv420p[v]")
    filters.append("[mus]" + "".join(alabels) + f"amix=inputs={len(alabels) + 1}:normalize=0:duration=first,"
                   f"alimiter=limit=0.9:level=disabled,apad=whole_dur={total:.3f},atrim=0:{total:.3f}[a]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k", str(dest)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = media.run_watched(cmd, total * 4, label="Cutting the montage to the beat", log=log, stall=120,
                             timeout=1800)
    if proc.returncode != 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise media.MediaError((proc.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:300])
    media._PROBE_CACHE.pop(dest, None)
    return dest, frames


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------

def pick_song(tracks: list, history: dict, seed: int = 0, used: set[str] = frozenset()):
    """An upbeat song with a steady beat: high or medium energy, a BPM tag of 90-150 if possible,
    not used recently."""
    import time
    week = time.time() - 7 * 86400

    def score(t):
        bpm = _bpm(getattr(t, "bpm", ""))
        e = (getattr(t, "energy", "") or "").lower()
        return ((2 if e == "high" else 1 if e == "medium" else 0) + (2 if bpm and 90 <= bpm <= 150 else 0)
                - (3 if history.get(t.path, 0) > week else 0) - (2 if t.path in used else 0))
    ranked = sorted(tracks, key=score, reverse=True)
    if not ranked:
        return None
    top = [t for t in ranked if score(t) == score(ranked[0])]
    return random.Random(seed).choice(top)


def menu_spans(sig, flat: float) -> list[tuple[float, float]]:
    """Time spans where a menu is open, from the keyframe measurements."""
    if sig is None or not sig.frames:
        return []
    out = []
    for k, (t, m) in enumerate(zip(sig.frames, sig.menu)):
        if m >= flat:
            nxt = sig.frames[k + 1] if k + 1 < len(sig.frames) else t + 4.0
            out.append((t - 1.0, nxt))
    return out


def hit_gains(rec: Path, stream: int | None, hits: list[float], target_db: float = -8.0) -> list[float]:
    """Per-hit gain so every hit peaks at about the same level."""
    import numpy as np
    gains = []
    for t in hits:
        try:
            x = decode(rec, t - 0.02, 0.25, stream)
            peak = float(np.abs(x).max()) if len(x) else 0.0
        except media.MediaError:
            peak = 0.0
        gains.append(round(min(max(target_db - 20 * math.log10(peak), -12.0), 18.0), 1) if peak > 1e-4 else 0.0)
    return gains


@dataclass
class Result:
    file: Path
    frames: int
    plan: Plan
    real_seconds: float
    note: str = ""


def block_hits(x, start: float, end: float, matcher, skip=()) -> list[tuple[float, float]]:
    """Sharp sounds in game audio `x` (decoded from `start`) that are blocks breaking or being
    placed, as (time, how well it matched Minecraft's sound). Eating, burps, pickups, damage,
    footsteps and mining ticks are left out."""
    env, efps = onset_envelope(x)
    out = []
    for t, _ in find_hits(env, efps, start, skip, min_gap=0.08, loose=True):
        if not (start + PRE <= t <= end - HIT_WINDOW):
            continue
        score, _, at = matcher.locate(x, int(round((t - start) * SR)))
        if score >= matcher.threshold:
            out.append((round(start + at / SR, 4), score))     # exact start from the waveform match
    return out


def make(rec: Path, game_stream: int | None, start: float, end: float, song, fps, width: int, height: int,
         dest: Path, music_db: float, sig=None, target: float = 15.0, matcher=None, log=print) -> Result | None:
    """Build a montage from rec[start:end] to `song` (a music.Track), cutting only on blocks
    breaking or being placed (`matcher`: mcsounds.Matcher). None when there's no matcher or
    the stretch doesn't have enough block sounds (the caller falls back to a fast-forward)."""
    from kdcut import signals
    if matcher is None:
        log("Montage skipped: Minecraft's sound files weren't found, so block sounds can't be told apart")
        return None
    x = decode(rec, start, end - start, game_stream)
    hits = block_hits(x, start, end, matcher, menu_spans(sig, signals.MENU_FLAT))
    if len(hits) < MIN_HITS:
        log(f"Montage skipped: only {len(hits)} block sounds in that stretch")
        return None
    song_len = float(getattr(song, "duration", 0) or 0) or media.probe(Path(song.path)).duration
    window = min(song_len, 90.0)
    m = decode(Path(song.path), 0.0, window)
    menv, mfps = onset_envelope(m, low_only=True)
    period, phase = beat_grid(menv, mfps, _bpm(getattr(song, "bpm", "")))
    plan = lay_out(hits, Path(song.path), song.name, period, phase, window, target)
    if plan is None:
        log("Montage skipped: the hits are too close together to fill the beats")
        return None
    gains = hit_gains(rec, game_stream, plan.hits)
    file, frames = render(plan, rec, game_stream, dest, fps, width, height, music_db, gains, log=log)
    return Result(file, frames, plan, end - start,
                  f"{len(plan.hits)} cuts on the beat of {song.name} ({plan.bpm:g} BPM)")
