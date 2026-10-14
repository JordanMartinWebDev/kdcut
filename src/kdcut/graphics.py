"""Small rendered graphics for the timeline: on-screen text labels and the "Coming up" teaser.

Resolve doesn't reliably pick up effects from an imported FCPXML, so anything that has to
look a certain way (black and white, text, a flash) is rendered by KDcut into its own file.
"""

from __future__ import annotations

from pathlib import Path

from kdcut import media


def _font(size: int):
    from kdcut.thumbnails import _font as thumb_font
    return thumb_font(None, size)


def label_png(text: str, dest: Path, width: int = 1920, height: int = 1080, y: float = 0.70) -> Path:
    """Big bold label (white, black outline, soft shadow) on a transparent full frame,
    centred horizontally at `y` of the height, e.g. 'Iron Farm' under an item spin."""
    from PIL import Image, ImageDraw, ImageFilter
    scale = height / 1080
    text = text.strip().upper()
    size = int(120 * scale)
    font = _font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while size > 40 and probe.textbbox((0, 0), text, font=font)[2] > width * 0.8:
        size -= 6
        font = _font(size)
    l, t, r, b = probe.textbbox((0, 0), text, font=font, stroke_width=max(int(size * 0.08), 2))
    x = (width - (r - l)) // 2 - l
    yy = int(height * y - (b - t) / 2) - t
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x + int(6 * scale), yy + int(8 * scale)), text, font=font,
                                fill=(0, 0, 0, 170), stroke_width=max(int(size * 0.08), 2),
                                stroke_fill=(0, 0, 0, 170))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6 * scale)))
    ImageDraw.Draw(img).text((x, yy), text, font=font, fill=(255, 255, 255, 255),
                             stroke_width=max(int(size * 0.08), 2), stroke_fill=(0, 0, 0, 255))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def coming_up_png(dest: Path, width: int = 1920, height: int = 1080, text: str = "COMING UP") -> Path:
    """'COMING UP' tag for the top-left corner, on a dark band so it reads on any footage."""
    from PIL import Image, ImageDraw
    scale = height / 1080
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(int(96 * scale))
    pad_x, pad_y = int(70 * scale), int(60 * scale)
    l, t, r, b = d.textbbox((0, 0), text, font=font, stroke_width=int(6 * scale))
    box = (pad_x - int(28 * scale), pad_y - int(18 * scale),
           pad_x + (r - l) + int(28 * scale), pad_y + (b - t) + int(22 * scale))
    d.rectangle(box, fill=(0, 0, 0, 150))
    d.rectangle((box[0], box[3] - int(8 * scale), box[2], box[3]), fill=(255, 210, 40, 255))
    d.text((pad_x - l, pad_y - t), text, font=font, fill=(255, 255, 255, 255),
           stroke_width=int(6 * scale), stroke_fill=(0, 0, 0, 255))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def render_teaser(src: Path, start: float, frames: int, fps, dest: Path, width: int, height: int,
                  log=None, flash: float = 0.2) -> Path:
    """The cold open as its own clip: black and white, 'COMING UP' in the corner, and a quick
    flash to white at the end that leads into the episode. Keeps the recording's audio."""
    seconds = frames / float(fps)
    tag = coming_up_png(dest.with_name(dest.stem + "_tag.png"), width, height)
    vf = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
          "hue=s=0,eq=contrast=1.12:brightness=-0.02[bw];"
          f"[bw][1:v]overlay=0:0:format=auto,"
          f"fade=t=out:st={max(seconds - flash, 0):.3f}:d={flash:.3f}:color=white,format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-loop", "1", "-i", str(tag),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-frames:v", str(frames),
           "-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
           "-c:a", "aac", "-b:a", "192k", str(dest)]
    proc = media.run_watched(cmd, seconds, label="Rendering the teaser", log=log, stall=60,
                             timeout=600)
    if proc.returncode != 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise media.MediaError((proc.stderr.strip().splitlines() or ["ffmpeg failed"])[0][:300])
    media._PROBE_CACHE.pop(dest, None)
    return dest
