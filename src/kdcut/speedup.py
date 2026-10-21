"""Fast-forward: a quiet stretch shown at 2-12x instead of cut, so viewers see the progress.

The AI asks for one after a block ("the 3-minute walk back to base, at 8x"). KDcut renders
that stretch of the recording sped up, muted (the music plays under it) with a small fast-
forward badge in the corner, and puts it in the story right after the block.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

from kdcut import media
from kdcut.blocks import Block


def source_span(block: Block, nxt: Block | None, seconds: float, edit: dict, extend: float,
                duration: float, voice=None) -> tuple[float, float] | None:
    """Where in the recording the sped-up stretch is: right after the block (and its kept
    extend), never running into the next thing said."""
    end_of_talk = voice.speech_end(block.end) if voice is not None else block.end
    start = end_of_talk + float(edit.get("pad_after", 0.3)) + extend
    limit = min(duration, (nxt.start - 0.5) if nxt is not None else duration)
    end = min(start + seconds, limit)
    if end - start < 4.0:
        return None
    return start, end


def badge_png(dest: Path, speed: float, width: int, height: int) -> Path:
    """'▶▶ 4×' in a dark pill, top right."""
    from PIL import Image, ImageDraw
    from kdcut.graphics import _font
    s = height / 1080
    text = f"{speed:g}x" if speed != int(speed) else f"{int(speed)}x"
    font = _font(int(54 * s))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tri = int(30 * s)
    pad = int(20 * s)
    w = pad + tri * 2 + int(10 * s) + (r - l) + pad
    h = max(b - t, tri) + pad * 2 // 1
    x0, y0 = width - w - int(48 * s), int(44 * s)
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=h // 2, fill=(0, 0, 0, 150))
    cy = y0 + h // 2
    for k in range(2):
        tx = x0 + pad + k * tri
        d.polygon([(tx, cy - tri // 2), (tx + tri, cy), (tx, cy + tri // 2)], fill=(255, 255, 255, 235))
    d.text((x0 + pad + tri * 2 + int(10 * s) - l, cy - (b - t) // 2 - t), text, font=font, fill=(255, 255, 255, 255))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def render(src: Path, start: float, end: float, speed: float, dest: Path, fps: Fraction,
           width: int, height: int, badge: bool = True, log=None) -> tuple[Path, int]:
    """Render the sped-up stretch. Returns (file, frames on the timeline)."""
    out_secs = (end - start) / speed
    frames = max(int(round(out_secs * float(fps))), 1)
    sig = f"{src}|{start:.3f}|{end:.3f}|{speed}|{fps}|{width}x{height}|{badge}|v1"
    dest = dest.with_name(f"{dest.stem}_{hashlib.sha1(sig.encode()).hexdigest()[:10]}.mp4")
    if dest.is_file():
        return dest, frames
    vf = (f"[0:v]setpts=(PTS-STARTPTS)/{speed},scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src)]
    if badge:
        tag = badge_png(dest.with_name(f"badge_{speed:g}x.png"), speed, width, height)
        cmd += ["-loop", "1", "-i", str(tag)]
        vf += "[v];[v][1:v]overlay=0:0:format=auto,format=yuv420p[o]"
    else:
        vf += ",format=yuv420p[o]"
    cmd += ["-filter_complex", vf, "-map", "[o]", "-an", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", str(dest)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = media.run_watched(cmd, end - start, label="Speeding up a quiet stretch", log=log, stall=90,
                             timeout=1800)
    if proc.returncode != 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise media.MediaError((proc.stderr.strip().splitlines() or ["ffmpeg failed"])[0][:300])
    media._PROBE_CACHE.pop(dest, None)
    return dest, frames


def silent_clips(blocks: list[Block], n_recordings: int) -> dict[str, list[int]]:
    """Recordings with no talking at all, by the block they follow: the last line said before
    them. These clips were recorded on purpose ("start a montage of mining diamonds", then a
    few silent clips of mining), so they belong to that line's quiet stretch."""
    with_talk = sorted({b.source for b in blocks})
    last_block = {}
    for b in blocks:
        last_block[b.source] = b.id
    out: dict[str, list[int]] = {}
    for a, z in zip(with_talk, with_talk[1:] + [n_recordings]):
        gap = list(range(a + 1, z))
        if gap:
            out[last_block[a]] = gap
    return out


def join_clips(paths: list[Path], dest: Path, log=print) -> Path:
    """The silent clips as one file (stream copy: no re-encode, so it's quick). Recordings
    from the same OBS setup share their format, which is what joining without re-encoding
    needs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    key = "|".join(f"{p}:{p.stat().st_size}" for p in paths)
    stamp = dest.with_suffix(".key")
    if dest.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == key:
        return dest
    lst = dest.with_suffix(".txt")
    lst.write_text("".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in paths),
                   encoding="utf-8")
    media._run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-map", "0", "-c", "copy", str(dest)])
    stamp.write_text(key, encoding="utf-8")
    return dest
