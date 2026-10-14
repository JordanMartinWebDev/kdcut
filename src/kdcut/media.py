"""ffmpeg/ffprobe helpers."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".flv", ".webm", ".m4v", ".avi"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class MediaError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise MediaError(
                f"{tool} was not found on PATH. Install it with `winget install Gyan.FFmpeg` "
                "and open a new terminal."
            )


@dataclass
class AudioStream:
    index: int            # 0-based audio stream index (ffmpeg -map 0:a:N)
    channels: int
    title: str = ""


@dataclass
class MediaInfo:
    path: Path
    duration: float
    has_video: bool
    width: int = 0
    height: int = 0
    fps: Fraction | None = None
    audio: list[AudioStream] = field(default_factory=list)
    is_image: bool = False
    vcodec: str = ""
    pix_fmt: str = ""
    webm_alpha: bool = False   # VP8/VP9 WebM with an alpha layer
    tags: dict = field(default_factory=dict)   # container tags, lowercased keys (ID3 etc.)

    @property
    def has_alpha(self) -> bool:
        return self.webm_alpha or any(k in self.pix_fmt for k in ("yuva", "rgba", "argb", "bgra", "abgr", "ya8", "ya16", "pal8"))

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ff(cmd: list[str]) -> list[str]:
    """ffmpeg must never wait on the keyboard: from a desktop app its stdin can be a stray
    handle, and ffmpeg polling it can stall a job forever."""
    return [cmd[0], "-nostdin", *cmd[1:]] if Path(cmd[0]).stem.lower() == "ffmpeg" else cmd


def run_quiet(cmd: list[str], timeout: float | None = None, text: bool = True) -> subprocess.CompletedProcess:
    kw = dict(encoding="utf-8", errors="replace") if text else {}
    return subprocess.run(ff(cmd), capture_output=True, text=text, stdin=subprocess.DEVNULL,
                          timeout=timeout, creationflags=NO_WINDOW, **kw)


def _run(cmd: list[str]) -> str:
    proc = run_quiet(cmd)
    if proc.returncode != 0:
        raise MediaError(f"{cmd[0]} failed: {proc.stderr.strip()[-800:]}")
    return proc.stdout


_PROBE_CACHE: dict[Path, MediaInfo] = {}


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    if path in _PROBE_CACHE:
        return _PROBE_CACHE[path]
    out = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    data = json.loads(out)
    streams = data.get("streams", [])
    is_image = path.suffix.lower() in IMAGE_EXTS
    duration = float(data.get("format", {}).get("duration") or 0.0)
    info = MediaInfo(path=path, duration=duration, has_video=False, is_image=is_image,
                     tags={str(k).lower(): str(v) for k, v in (data.get("format", {}).get("tags") or {}).items()})
    for s in streams:
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1:
            if not info.has_video:
                info.has_video = True
                info.width = int(s.get("width") or 0)
                info.height = int(s.get("height") or 0)
                info.vcodec = s.get("codec_name", "")
                info.pix_fmt = s.get("pix_fmt", "") or ""
                info.webm_alpha = str((s.get("tags") or {}).get("alpha_mode", "")) == "1"
                rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
                try:
                    fr = Fraction(rate)
                except (ValueError, ZeroDivisionError):
                    fr = Fraction(0)
                info.fps = fr if fr > 0 else None
                if not duration and s.get("duration"):
                    info.duration = float(s["duration"])
        elif s.get("codec_type") == "audio":
            info.audio.append(AudioStream(
                index=len(info.audio),
                channels=int(s.get("channels") or 2),
                title=(s.get("tags") or {}).get("title", "") or "",
            ))
    _PROBE_CACHE[path] = info
    return info


def normalize_fps(fps: Fraction | None) -> Fraction:
    """Snap measured rates (59.94xxx from VFR OBS files) to standard timeline rates."""
    if not fps:
        return Fraction(60)
    standard = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
                Fraction(30), Fraction(50), Fraction(60000, 1001), Fraction(60),
                Fraction(120), Fraction(144)]
    return min(standard, key=lambda s: abs(float(s) - float(fps)))


def parse_fps(value) -> Fraction | None:
    if value in (None, "", "auto"):
        return None
    v = float(value)
    for s in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)):
        if abs(float(s) - v) < 0.01:
            return s
    return Fraction(value).limit_denominator(1001)


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def list_media(folder: Path, exts: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts
             and not p.name.startswith(".")]
    return sorted(files, key=_natural_key)


def pick_mic_track(info: MediaInfo, setting, log=None) -> int:
    """Return the 0-based audio stream to transcribe. A track number that this recording
    doesn't have (e.g. 'track 2' on a one-track MP4) falls back instead of failing."""
    if not info.audio:
        raise MediaError(f"{info.path.name} has no audio track to transcribe.")
    if isinstance(setting, int) or (isinstance(setting, str) and setting.isdigit()):
        idx = int(setting) - 1
        if 0 <= idx < len(info.audio):
            return idx
        if log:
            n = len(info.audio)
            log(f"Settings say your mic is on audio track {setting}, but {info.path.name} only has "
                f"{n} track{'s' if n != 1 else ''}. Using "
                + ("that one (voice and game together)." if n == 1 else "the best guess instead."))
    for a in info.audio:
        if "mic" in a.title.lower():
            return a.index
    return 0


def extract_audio(src: Path, stream: int, dest: Path) -> Path:
    """16 kHz mono WAV of one audio stream, which is what Whisper wants."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", f"0:a:{stream}",
          "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)])
    return dest


def extract_frame(src: Path, at: float, dest: Path, width: int | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", f"scale={width}:-2"] if width else []
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(at, 0):.3f}", "-i", str(src),
          "-frames:v", "1", *vf, "-q:v", "3", "-update", "1", str(dest)])
    return dest


def corner_color(path: Path, frame: int = 5) -> tuple[int, int, int] | None:
    """Average colour of the top-left corner of one frame (used to spot green screens)."""
    try:
        proc = run_quiet(
            ["ffmpeg", "-v", "error", "-i", str(path), "-vf",
             f"select=gte(n\\,{frame}),crop=16:16:0:0,scale=1:1", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            timeout=60, text=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(proc.stdout) < 3:
        return None
    return proc.stdout[0], proc.stdout[1], proc.stdout[2]


def is_green_screen(rgb) -> bool:
    if not rgb:
        return False
    r, g, b = rgb
    return g > 180 and r < 110 and b < 110


def _to_prores4444(src: Path, dest: Path, pre: list[str], vf: str | None) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", *pre, "-i", str(src)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-an", str(dest)]
    _run(cmd)


# No despill: it turns cyan items (diamonds!) blue, and pixel art has hard edges anyway.
GREEN_KEY = "chromakey=0x00FF00:0.18:0.04"


def prepare_overlay(src: Path, cache_dir: Path) -> tuple[Path, str]:
    """Make an editing-pack overlay drop straight onto a Resolve timeline.

    Returns (path_to_use, what_was_done). Handles the three formats packs ship in:
      * green-screen MP4s (most item animations and transitions): keyed to transparent ProRes
      * WebM/GIF with transparency, which Resolve free can't read reliably: ProRes 4444
      * ProRes/PNG that already has alpha: used as-is
    """
    src = Path(src)
    info = probe(src)
    ext = src.suffix.lower()
    if info.is_image and ext != ".webp":
        return src, "as-is"
    if ext not in RESOLVE_UNFRIENDLY and info.has_alpha:
        return src, "as-is"

    tag = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:8]
    cache_dir.mkdir(parents=True, exist_ok=True)

    if ext == ".webp" and info.duration <= 0:
        dest = cache_dir / f"{src.stem}-{tag}.png"
        if not dest.exists():
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), str(dest)])
        return dest, "converted"

    dest = cache_dir / f"{src.stem}-{tag}.mov"
    green = not info.has_alpha and is_green_screen(corner_color(src))
    action = "keyed" if green else "converted"
    if ext not in RESOLVE_UNFRIENDLY and not green:
        return src, "as-is"   # opaque clip, nothing to fix
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest, action

    pre: list[str] = []
    if info.webm_alpha:
        # ffmpeg's built-in VP8/VP9 decoders drop the alpha layer; libvpx keeps it.
        pre = ["-c:v", "libvpx-vp9" if info.vcodec == "vp9" else "libvpx"]
    try:
        _to_prores4444(src, dest, pre, GREEN_KEY if green else None)
    except MediaError:
        if not pre:
            raise
        _to_prores4444(src, dest, [], GREEN_KEY if green else None)
        action += " (alpha lost: ffmpeg build lacks libvpx decoder)"
    _PROBE_CACHE.pop(dest, None)
    return dest, action


RESOLVE_UNFRIENDLY = {".webm", ".gif", ".webp"}
