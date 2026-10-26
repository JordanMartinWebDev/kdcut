"""Get every audio track of a recording into Resolve, at sensible levels.

Resolve only brings in the first audio track of a multi-track MKV/MP4 when it imports an
FCPXML timeline, so an OBS recording with game on track 1 and your mic on track 2 arrives
without your voice. KDcut therefore pulls each track out to its own file (voice and game
separately), levels them, and lays them under the video as their own audio tracks:

    A1 voice (or voice + game, for a one-track recording)   about -16 LUFS
    A2 game audio                                           about -30 LUFS, under the voice

Sound effects are levelled the same way so a quiet click and a loud explosion from two
different packs don't land 20 dB apart.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kdcut import media
from kdcut.config import HOME_DIR

VOICE_LUFS = -16.0
GAME_LUFS = -30.0
SFX_LUFS = -20.0
LOUDNESS_CACHE = HOME_DIR / "loudness.json"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ffmpeg(cmd: list[str], timeout: float = 900) -> subprocess.CompletedProcess:
    try:
        return media.run_quiet(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timed out")


def measure(path: Path, stream: int | None = None, timeout: float = 900, *, total: float | None = None,
            label: str = "Measuring", log=None) -> tuple[float | None, float | None]:
    """(integrated loudness in LUFS, true peak in dBTP). None when silent or unmeasurable.
    With `total` (the file's length) the job is watched: progress is logged and a stall stops it."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path)]
    if stream is not None:
        cmd += ["-map", f"0:a:{stream}"]
    # apad: very short sounds (a 0.5 s pop) are too short for an integrated reading otherwise.
    cmd += ["-vn", "-af", "apad=pad_dur=1,loudnorm=print_format=json", "-f", "null", "-"]
    if total:
        proc = media.run_watched(cmd, total, label=label, log=log, timeout=timeout)
    else:
        proc = _ffmpeg(cmd, timeout=timeout)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr)
    if not m:
        return None, None
    data = json.loads(m[-1])

    def num(key):
        try:
            v = float(data.get(key))
        except (TypeError, ValueError):
            return None
        return v if v > -70 else None

    return num("input_i"), num("input_tp")


def gain_for(lufs: float | None, peak: float | None, target: float,
             lo: float = -24.0, hi: float = 18.0, over: float = 12.0) -> float:
    """Gain to reach `target`, letting peaks go at most `over` dB past -1 dBTP
    (a limiter catches those; use over=0 where there's no limiter)."""
    if lufs is None:
        return 0.0
    g = target - lufs
    if peak is not None:
        g = min(g, -1.0 - peak + over)
    return round(max(lo, min(hi, g)), 1)


# --------------------------------------------------------------------------------------
# Sound effects: a clip gain per file, cached
# --------------------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(LOUDNESS_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def sfx_gain(path: Path, target: float = SFX_LUFS) -> float:
    """dB to apply to a sound effect so it sits at a consistent loudness."""
    path = Path(path)
    cache = _load_cache()
    try:
        st = path.stat()
    except OSError:
        return 0.0
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    if key not in cache:
        lufs, peak = measure(path)
        cache[key] = [lufs, peak]
        try:
            LOUDNESS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            LOUDNESS_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    lufs, peak = cache[key]
    return gain_for(lufs, peak, target, lo=-24.0, hi=12.0, over=0.0)


MUSIC_REF_LUFS = -14.0   # every track is brought to this first; the volume setting is relative to it


def music_gain(path: Path) -> float:
    """dB that brings a music track to MUSIC_REF_LUFS (tracks from different sources differ a lot)."""
    return sfx_gain(path, target=MUSIC_REF_LUFS)


# --------------------------------------------------------------------------------------
# Recordings: one levelled audio file per track
# --------------------------------------------------------------------------------------

@dataclass
class Stem:
    kind: str        # voice | game | mix
    label: str       # what the clip is called in Resolve
    path: Path
    gain_db: float


def split_tracks(rec: Path, info: media.MediaInfo, mic: int, dest_dir: Path, log=print) -> list[Stem]:
    """Write each audio track of `rec` to its own levelled .m4a. Re-uses earlier results."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    st = rec.stat()
    plan: list[tuple[int, str, str, float]] = []
    if len(info.audio) == 1:
        plan.append((0, "mix", "Voice + game", VOICE_LUFS))
    else:
        plan.append((mic, "voice", "Voice", VOICE_LUFS))
        for a in info.audio:
            if a.index != mic:
                n = sum(1 for k in plan if k[1] == "game")
                plan.append((a.index, "game", "Game" + (f" {n + 1}" if n else ""), GAME_LUFS))
    stems = []
    limit = 300 + info.duration       # generous; a stuck ffmpeg must not hang the whole build
    for stream, kind, label, target in plan:
        base = f"{rec.stem}.a{stream + 1}.{kind}"
        meta = dest_dir / f"{base}.json"
        sig = {"size": st.st_size, "mtime": int(st.st_mtime), "stream": stream, "target": target, "v": 2}
        try:
            old = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        done = Path(old.get("file") or dest_dir / f"{base}.m4a")
        if old.get("sig") == sig and done.is_file():
            stems.append(Stem(kind, label, done, old.get("gain", 0.0)))
            continue
        name = label.lower()
        log(f"Levelling {name} audio from {rec.name} (track {stream + 1})")
        lufs, peak = measure(rec, stream, timeout=limit, total=info.duration,
                             label=f"Measuring {name} audio", log=log)
        media.debug(f"{name}: {lufs} LUFS, peak {peak}")
        gain = gain_for(lufs, peak, target, over=12.0 if kind in ("voice", "mix") else 0.0)
        af = f"volume={gain}dB"
        if kind in ("voice", "mix"):
            af += ",alimiter=limit=0.89:level=disabled"      # catch shouts after the boost
        src = ["ffmpeg", "-y", "-v", "error", "-i", str(rec), "-map", f"0:a:{stream}", "-vn"]
        # async: OBS tracks can have gaps (e.g. the game paused); fill them with silence so
        # everything after stays in sync.
        wav = (dest_dir / f"{base}.wav", ["-af", f"aresample=48000:async=1:first_pts=0,{af}",
                                          "-c:a", "pcm_s16le", "-rf64", "auto"])
        aac = (dest_dir / f"{base}.m4a", ["-af", f"aresample=48000:async=1:first_pts=0,{af}",
                                          "-c:a", "aac", "-b:a", "256k"])
        # Game tracks go silent whenever the game is paused or quiet, and ffmpeg's AAC encoder
        # can freeze on long digital silence (it stalled on 3 of Episode 2's 35 game tracks,
        # costing a minute each before the WAV fallback). WAV never does, so game audio is
        # written as WAV straight away. The voice track always has some mic noise: AAC first.
        attempts = [wav] if kind == "game" else [aac, wav]
        out, why = None, ""
        for i, (dest, opts) in enumerate(attempts):
            proc = media.run_watched(src + opts + [str(dest)], info.duration, log=log, timeout=limit,
                                     label=f"Writing {name} audio" + (" (plan B)" if i else ""))
            if proc.returncode == 0 and dest.is_file():
                out = dest
                break
            dest.unlink(missing_ok=True)
            why = (proc.stderr.strip().splitlines() or ["unknown error"])[0][:200]
            log(f"Writing {name} audio failed: {why}" + ("; trying another way" if i + 1 < len(attempts) else ""))
        if out is None:
            if kind == "game":       # the recording's own audio still carries track 1
                log(f"Skipped levelling game audio from {rec.name}; using the recording's own audio")
                continue
            raise media.MediaError(f"Couldn't extract audio track {stream + 1} from {rec.name}: {why}")
        meta.write_text(json.dumps({"sig": sig, "gain": gain, "lufs": lufs, "file": str(out)}), encoding="utf-8")
        stems.append(Stem(kind, label, out, gain))
    return stems
