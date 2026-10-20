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
PRE = 0.02          # each slice starts this long before its hit: the cut lands right on the beat
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


def kick_phase(x, period: float, phase: float) -> float:
    """Beat or off-beat? When a song's onsets are as strong between the beats as on them
    (driving eighth notes), the kick drum decides: the beat is where it hits hardest."""
    import numpy as np
    n = len(x)
    if n < SR * 4:
        return phase
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / SR)
    kick = np.fft.irfft(X * ((f > 40) & (f < 110)), n)
    hop = int(0.005 * SR)
    e = np.sqrt(np.convolve(kick ** 2, np.ones(hop) / hop, "valid")[::hop])

    def strength(ph):
        vals = [e[int(t / 0.005): int(t / 0.005) + 12].max() for t in np.arange(ph, n / SR - 0.1, period)
                if int(t / 0.005) + 12 <= len(e)]
        return float(np.mean(vals)) if vals else 0.0
    on, off = strength(phase), strength(phase + period / 2)
    return phase + period / 2 if off > on * 1.15 else phase


def loud_starts(x, period: float, phase: float, beats_per_bar: int, bar_offset: int) -> list[float]:
    """Downbeats to start a montage on, best first: where a loud section of the song begins
    (the drop after an intro or a quiet bridge), then anywhere loud, then the rest. A montage
    in the song's quiet intro feels flat however well it's cut."""
    import numpy as np
    bar = beats_per_bar * period
    first = phase + bar_offset * period
    bars = []
    t = first
    while t + bar <= len(x) / SR:
        a, b = int(t * SR), int((t + bar) * SR)
        bars.append((t, 20 * float(np.log10(np.sqrt(np.mean(x[a:b] ** 2)) + 1e-9))))
        t += bar
    if not bars:
        return []
    loud = float(np.percentile([db for _, db in bars], 85))
    out_drop, out_loud, out_rest = [], [], []
    for k, (t, db) in enumerate(bars):
        ahead = [d for _, d in bars[k:k + 4]]
        if min(ahead) >= loud - 3:
            if k > 0 and bars[k - 1][1] < loud - 4:
                out_drop.append(t)
            else:
                out_loud.append(t)
        else:
            out_rest.append(t)
    return out_drop + out_loud + out_rest


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


@dataclass
class Segment:
    start: float                    # seconds on the montage clip
    dur: float
    src: float                      # where it starts in the recording (file timeline)
    speed: float = 1.0              # > 1: a fast-forward shot
    hit: float | None = None        # the block sound this slice is cut on (lands on a beat)
    audio: float = 0.0              # seconds of game audio kept from `src`


# Bars of the song, in beats. "hit" slices start just before a block sound so it lands on
# the beat; "ramp" is one fast-forward shot that lands on the next beat with a hit.
BARS = {
    "chop":   [("hit", 1)] * 4,
    "double": [("hit", 0.5)] * 8,
    "build":  [("hit", 1), ("hit", 1)] + [("hit", 0.5)] * 4,
    "ramp":   [("ramp", 2), ("hit", 1), ("hit", 1)],
    "launch": [("ramp", 2)] + [("hit", 0.5)] * 4,
}
# Starts on beat cuts, speeds up, and keeps changing so it never feels mechanical.
SEQUENCE = ["chop", "build", "ramp", "double", "launch", "double", "build", "ramp", "double", "chop"]
RAMP_SPEED = 4.0


@dataclass
class Plan:
    song: Path
    song_name: str
    song_start: float               # seconds into the song where the montage starts
    segments: list[Segment]
    bpm: float
    bars: list[str]

    @property
    def seconds(self) -> float:
        return self.segments[-1].start + self.segments[-1].dur

    @property
    def hits(self) -> list[float]:
        return [g.hit for g in self.segments if g.hit is not None]

    @property
    def beats(self) -> list[float]:
        """Where each block sound lands on the montage clip (all on the song's beat grid)."""
        return [g.start + PRE for g in self.segments if g.hit is not None]

    @property
    def ramps(self) -> int:
        return sum(1 for g in self.segments if g.speed > 1)


def downbeat(env, fps: float, period: float, phase: float) -> int:
    """Which beat of four starts a bar: the one the song accents most (kick and snare
    patterns repeat every bar). 0-3, counted from `phase`."""
    import numpy as np
    acc = []
    for j in range(4):
        idx = []
        k = j
        while True:
            t = phase + k * period - 0.75 * WIN / SR
            i = int(round(t * fps))
            if i >= len(env):
                break
            if i >= 0:
                idx.append(i)
            k += 4
        acc.append(float(np.mean(env[idx])) if idx else 0.0)
    return int(np.argmax(acc))


def pattern(n_bars: int, half_ok: bool, ramps_ok: bool = True) -> list[str]:
    out = []
    for name in SEQUENCE:
        if len(out) >= n_bars:
            break
        if not half_ok and name in ("double", "build", "launch"):
            name = "chop" if name != "launch" else "ramp"
        if not ramps_ok and name in ("ramp", "launch"):
            name = "double" if half_ok else "chop"
        out.append(name)
    while len(out) < n_bars:
        out.append(out[len(out) % len(SEQUENCE)])
    return out


def allocate(spec: list[tuple[str, float]], hits: list[float], sounds: list[float], first: int
             ) -> list[Segment] | None:
    """Give each slice its footage, in recording order, never reusing any. `hits` are the block
    sounds to cut on (sorted), `sounds` every block sound found (so a slice's game audio stops
    before the next one, which would land between the beats), `first` the hit to start from."""
    import bisect
    segs: list[Segment] = []
    t_clip = 0.0
    j = first
    cursor = hits[first] - PRE - 1e-6 if hits else 0.0
    for kind, dur in spec:
        if kind == "hit":
            while j < len(hits) and hits[j] - PRE < cursor:
                j += 1
            if j >= len(hits):
                return None
            h = hits[j]
            src = h - PRE
            k = bisect.bisect_right(sounds, h + 0.03)
            nxt = sounds[k] if k < len(sounds) else None
            audio = min(PRE + HIT_WINDOW, dur, (nxt - src - 0.02) if nxt is not None else 1e9)
            segs.append(Segment(t_clip, dur, src, 1.0, h, max(audio, PRE + 0.06)))
            cursor = src + dur
            j += 1
        else:
            src = cursor + 0.05
            segs.append(Segment(t_clip, dur, src, RAMP_SPEED))
            cursor = src + dur * RAMP_SPEED
        t_clip += dur
    return segs


def active(times: list[float], near: float = 5.0, company: int = 2) -> list[float]:
    """Block sounds that are part of real building or mining: at least `company` others within
    `near` seconds. A lone one (a log broken between bites of food) doesn't count."""
    import bisect
    out = []
    for t in times:
        lo, hi = bisect.bisect_left(times, t - near), bisect.bisect_right(times, t + near)
        if hi - lo - 1 >= company:
            out.append(t)
    return out


def spread(names: list[str], u: float, times: list[float], sounds: list[float]) -> list[Segment] | None:
    """Lay the bars across the whole stretch so the montage shows progress from start to
    finish. The block sounds are shared out in order (bar 1 gets the first share, the last bar
    the last), so idle time (eating, walking, sorting chests, AFK) holds no slices and is
    skipped; within its share a bar's cuts are spread out too. A fast-forward shot shows the lead-up
    to the next cut at 4x."""
    import bisect
    n_bars = len(names)
    times = active(times)
    if not times:
        return None
    bounds = [round(k * len(times) / n_bars) for k in range(n_bars + 1)]
    segs: list[Segment] = []
    t_clip = 0.0
    cursor = times[0] - PRE - 1e-6
    for b, name in enumerate(names):
        share = times[bounds[b]:bounds[b + 1]]
        spec = [(kind, beats * u) for kind, beats in BARS[name]]
        m = sum(1 for kind, _ in spec if kind == "hit")
        if len(share) < m:
            return None
        aims = [share[int(k * len(share) / m)] for k in range(m)]    # evenly through the share
        k = 0
        for idx, (kind, dur) in enumerate(spec):
            if kind == "hit":
                lo = bisect.bisect_left(times, max(aims[k], cursor + PRE))
                if lo >= len(times):
                    return None
                h = times[lo]
                src = h - PRE
                nxt_i = bisect.bisect_right(sounds, h + 0.03)
                nxt = sounds[nxt_i] if nxt_i < len(sounds) else None
                audio = min(PRE + HIT_WINDOW, dur, (nxt - src - 0.02) if nxt is not None else 1e9)
                segs.append(Segment(t_clip, dur, src, 1.0, h, max(audio, PRE + 0.06)))
                cursor = src + dur
                k += 1
            else:
                # The lead-up to the next cut, sped up: that's where the building is happening
                # (from wherever the last cut ended could be walking or eating).
                target = (aims[k] if k < m else share[-1]) - PRE
                src = max(cursor + 0.05, target - dur * RAMP_SPEED)
                segs.append(Segment(t_clip, dur, src, RAMP_SPEED))
                cursor = src + dur * RAMP_SPEED
            t_clip += dur
    return segs


def _start(starts, fits, length, song_len, skip_intro):
    """The preferred downbeat (loud part of the song) that leaves room for the montage."""
    for b in starts or []:
        if b - PRE >= 0 and b + length + 0.3 <= song_len:
            return b
    return next((b for b in fits if b >= skip_intro), fits[-1])


def lay_out(hits: list[tuple[float, float]], song: Path, song_name: str, period: float, phase: float,
            song_len: float, target: float = 15.0, skip_intro: float = 12.0, bar_offset: int = 0,
            starts: list[float] | None = None) -> Plan | None:
    """Fit a montage of about `target` seconds in whole bars of the song, starting on a
    downbeat. Cuts on the beat, on half beats, speeding up, with fast-forward shots in between;
    for fast songs a 'beat' is two beats (slices under ~0.2 s strobe). Footage comes from the
    busiest run of block sounds."""
    u = period
    while u < 0.4:
        u *= 2
    half_ok = u / 2 >= 0.2
    bar = 4 * u
    times = [t for t, _ in hits]
    most = max(int(round(target / bar)), 2)
    # In order: with fast-forward shots, from the busiest run (a slightly shorter montage is
    # fine); then allowed to start a little earlier; only then without fast-forwards.
    attempts = [("spread", n) for n in range(most, 1, -1)]      # first choice: the whole stretch
    for ramps_ok in (True, False):
        fewest = max(2, int(most * 0.6)) if ramps_ok else 2
        for widen in (False, True):
            attempts += [(ramps_ok, widen, n) for n in range(most, fewest - 1, -1)]
    for attempt in attempts:
        if attempt[0] == "spread":
            n_bars = attempt[1]
            names = pattern(n_bars, half_ok, True)
            length = n_bars * bar
            downs = [phase + (bar_offset + 4 * k * round(u / period)) * period
                     for k in range(int(song_len / bar) + 1)]
            fits = [b for b in downs if b - PRE >= 0 and b + length + 0.3 <= song_len]
            segs = spread(names, u, times, times) if fits else None
            if segs:
                first_beat = _start(starts, fits, length, song_len, skip_intro)
                return Plan(song, song_name, first_beat - PRE, segs, round(60.0 / period, 1), names)
            continue
        ramps_ok, widen, n_bars = attempt
        names = pattern(n_bars, half_ok, ramps_ok)
        spec = [(kind, beats * u) for name in names for kind, beats in BARS[name]]
        need = sum(1 for kind, _ in spec if kind == "hit")
        if need < MIN_HITS or len(hits) < need:
            continue
        length = n_bars * bar
        downs = [phase + (bar_offset + 4 * k * round(u / period)) * period
                 for k in range(int(song_len / bar) + 1)]
        fits = [b for b in downs if b - PRE >= 0 and b + length + 0.3 <= song_len]
        if not fits:
            continue
        first_beat = _start(starts, fits, length, song_len, skip_intro)
        run = focus(hits, need, sum(d for _, d in spec) / len(spec))
        start = times.index(run[0][0]) if run else 0
        # Widening goes back at most 15 s: never as far as a stray sound minutes earlier.
        starts = [k for k in range(start, -1, -1) if times[start] - times[k] <= 15.0] if widen else [start]
        for i in starts:
            segs = allocate(spec, times, times, i)
            if segs:
                return Plan(song, song_name, first_beat - PRE, segs, round(60.0 / period, 1), names)
    return None


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------

def render(plan: Plan, rec: Path, game_stream: int | None, dest: Path, fps, width: int, height: int,
           music_db: float, hit_gains: list[float] | None = None, log=None, punch: float = 0.0) -> tuple[Path, int]:
    """One clip: the slices back to back, the song, and each slice's hit. Returns (file, frames)."""
    fps = Fraction(fps)
    segs = plan.segments
    cuts = [int(round(g.start * fps)) for g in segs] + [int(round(plan.seconds * fps))]
    frames = cuts[-1] - cuts[0]
    total = frames / float(fps)
    sig = (f"{rec}|{plan.song}|{plan.song_start:.3f}|{[(g.src, g.dur, g.speed, g.audio) for g in segs]}|"
           f"{game_stream}|{music_db}|{hit_gains}|{fps}|{width}x{height}|{punch}|v11")
    dest = dest.with_name(f"{dest.stem}_{hashlib.sha1(sig.encode()).hexdigest()[:10]}.mp4")
    if dest.is_file():
        return dest, frames
    cmd = ["ffmpeg", "-y", "-v", "error"]
    filters, vlabels, alabels = [], [], []
    gains = iter(hit_gains or [])
    for k, g in enumerate(segs):
        f = cuts[k + 1] - cuts[k]
        t0 = max(g.src, 0.0)
        # Rough seek, then exact trims on the file's own timestamps (same as decode()).
        cmd += ["-ss", f"{max(t0 - 3.0, 0):.3f}", "-copyts", "-i", str(rec)]
        speed = f",setpts=PTS/{g.speed:g}" if g.speed != 1 else ""
        # Punch: each beat cut starts zoomed in a little and settles back (a common montage look).
        zoom = (f",zoompan=z='1+{punch}*exp(-14*on/{float(fps):.4f})':d=1:s={width}x{height}:fps={fps}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'") if punch and g.hit is not None else ""
        filters.append(f"[{k}:v]trim=start={t0:.4f},setpts=PTS-STARTPTS{speed},fps={fps},"
                       f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,trim=end_frame={f},"
                       f"setpts=PTS-STARTPTS{zoom}[v{k}]")
        vlabels.append(f"[v{k}]")
        if game_stream is not None and g.hit is not None:
            delay = int(round(cuts[k] / float(fps) * 1000))
            gain = next(gains, 0.0)
            fade = min(0.04, g.audio / 3)
            filters.append(f"[{k}:a:{game_stream}]atrim=start={t0:.4f}:end={t0 + g.audio:.4f},"
                           f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                           f"volume={gain:.1f}dB,afade=t=in:d=0.005,"
                           f"afade=t=out:st={max(g.audio - fade, 0):.3f}:d={fade:.3f},"
                           f"adelay={delay}|{delay}[h{k}]")
            alabels.append(f"[h{k}]")
    n = len(segs)
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
         dest: Path, music_db: float, sig=None, target: float = 15.0, matcher=None, log=print,
         punch: float = 0.0) -> Result | None:
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
    window = min(song_len, 300.0)
    m = decode(Path(song.path), 0.0, window)
    menv, mfps = onset_envelope(m, low_only=True)
    period, phase = beat_grid(menv, mfps, _bpm(getattr(song, "bpm", "")))
    phase = kick_phase(m, period, phase)
    fenv, _ = onset_envelope(m)
    offset = downbeat(fenv, mfps, period, phase)
    u_beats = 1
    while period * u_beats < 0.4:
        u_beats *= 2
    plan = lay_out(hits, Path(song.path), song.name, period, phase, window, target, bar_offset=offset,
                   starts=loud_starts(m, period, phase, 4 * u_beats, offset))
    if plan is None:
        log("Montage skipped: the hits are too close together to fill the beats")
        return None
    gains = hit_gains(rec, game_stream, plan.hits)
    file, frames = render(plan, rec, game_stream, dest, fps, width, height, music_db, gains, log=log, punch=punch)
    ramps = f" and {plan.ramps} fast-forward shots" if plan.ramps else ""
    return Result(file, frames, plan, end - start,
                  f"{len(plan.hits)} cuts{ramps} on the beat of {song.name} ({plan.bpm:g} BPM)")
