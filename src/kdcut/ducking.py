"""Music that gets quieter while you talk.

We already know exactly when you speak on the edited timeline, so no audio analysis is
needed: build a smooth volume envelope (down under your voice, back up in the gaps), mix
the chosen tracks into one music bed with ffmpeg, multiply by the envelope, and place that
single file on the music track in Resolve.
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from array import array
from pathlib import Path

from kdcut import media

RATE = 100   # envelope control rate (values per second)


def merge(intervals: list[tuple[float, float]], gap: float = 1.2) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def envelope(speech: list[tuple[float, float]], total: float, talk_db: float, gap_db: float,
             attack: float = 0.25, release: float = 0.8) -> list[float]:
    """Linear gain per 1/RATE second: talk_db under speech, gap_db elsewhere, with ramps
    that start *before* you speak (so the first word is never covered) and ease back up."""
    n = int(total * RATE) + 1
    db = [gap_db] * n
    depth = gap_db - talk_db
    for a, b in merge(speech):
        i0, i1 = max(int((a - attack) * RATE), 0), min(int(b * RATE), n - 1)
        for i in range(i0, i1 + 1):
            if i < a * RATE:   # ramp down during the attack
                frac = (i - (a - attack) * RATE) / max(attack * RATE, 1)
                db[i] = min(db[i], gap_db - depth * max(0.0, min(frac, 1.0)))
            else:
                db[i] = talk_db
        r1 = min(int((b + release) * RATE), n - 1)
        for i in range(i1 + 1, r1 + 1):   # ease back up after speaking
            frac = (i - b * RATE) / max(release * RATE, 1)
            db[i] = min(db[i], talk_db + depth * max(0.0, min(frac, 1.0)))
    return [10 ** (d / 20) for d in db]


def _write_env(gains: list[float], path: Path) -> None:
    pcm = array("h", (int(max(0.0, min(g, 1.0)) * 32767) for g in gains))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def render_bed(pieces: list, total: float, gains: list[float], dest: Path, start: float = 0.0) -> Path:
    """pieces: music.Placement items (timeline start, offset into track, length).
    Writes an AAC music bed covering [start, total) of the timeline."""
    if not pieces:
        raise ValueError("no music to render")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / "env.wav"
        _write_env(gains[int(start * RATE):], env)
        cmd = ["ffmpeg", "-y", "-v", "error"]
        filters = []
        for k, pc in enumerate(pieces):
            cmd += ["-ss", f"{pc.offset:.3f}", "-t", f"{pc.length:.3f}", "-i", pc.track.path]
            delay = int(max(pc.start - start, 0) * 1000)
            fade_out = max(pc.length - 2.0, 0)
            filters.append(f"[{k}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                           f"afade=t=in:d=1.5,afade=t=out:st={fade_out:.3f}:d=2,"
                           f"adelay={delay}|{delay}[m{k}]")
        n = len(pieces)
        cmd += ["-i", str(env)]
        length = max(total - start, 0.1)
        mix = "".join(f"[m{k}]" for k in range(n))
        filters.append(f"{mix}amix=inputs={n}:normalize=0:dropout_transition=0,"
                       f"apad=whole_dur={length:.3f},atrim=0:{length:.3f}[bed]")
        filters.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                       f"apad=whole_dur={length:.3f},atrim=0:{length:.3f}[env]")
        filters.append("[bed][env]amultiply[out]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
                "-c:a", "aac", "-b:a", "192k", str(dest)]
        try:
            proc = media.run_quiet(cmd, timeout=max(600.0, total * 2))
        except subprocess.TimeoutExpired as exc:
            raise media.MediaError("ffmpeg took too long mixing the music bed") from exc
        if proc.returncode != 0:
            raise media.MediaError(proc.stderr.strip()[-600:])
    media._PROBE_CACHE.pop(dest, None)
    return dest
