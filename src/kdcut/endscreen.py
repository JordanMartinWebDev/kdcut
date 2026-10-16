"""End screen: the last seconds of every video, laid out for YouTube's end screen elements.

YouTube only lets you place end screen elements (videos, playlist, subscribe) in Studio, and
the upload API can't set them. So KDcut renders the background with the spots marked, puts it
at the end of the timeline, and you add the elements once in Studio. After the first video,
"Import from video" copies them onto every new upload in one click.

Layout (1920x1080, scaled to the timeline size):
  - two video slots stacked on the left, each with a label above it
  - the subscribe circle in the middle
  - your character on the right, turned toward the slots
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from kdcut import thumbnails

REF_W, REF_H = 1920, 1080
# Element boxes in 1920x1080 space: (x, y, w, h). Video slots are 16:9.
SLOTS = [(100, 150, 704, 396), (100, 640, 704, 396)]
SUBSCRIBE = (868, 430, 220, 220)       # circle bounding box
DEFAULT_LABELS = ["WATCH NEXT", "MORE {series}"]


@dataclass
class EndScreen:
    image: Path
    slots: list[tuple[int, int, int, int]]
    subscribe: tuple[int, int, int, int]
    labels: list[str]


def _scale_box(box, sx, sy):
    x, y, w, h = box
    return int(x * sx), int(y * sy), int(w * sx), int(h * sy)


def _label_text(label: str, series: str) -> str:
    text = label.replace("{series}", series or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not series and "{series}" in label:
        text = "MORE VIDEOS"
    return text.upper()


def background(frame: Path | None, width: int, height: int, theme: thumbnails.Theme) -> Image.Image:
    """Blurred, darkened last frame of the episode (or a plain brand gradient without one)."""
    if frame and Path(frame).exists():
        bg = ImageOps.fit(Image.open(frame).convert("RGB"), (width, height), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(max(6, width // 120)))
        bg = ImageEnhance.Color(bg).enhance(1.15)
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
    else:
        top = thumbnails._hex(theme.primary)[:3]
        bg = Image.linear_gradient("L").resize((width, height))
        bg = ImageOps.colorize(bg, black=tuple(int(c * 0.55) for c in top), white=(18, 14, 10))
    return bg.convert("RGBA")


def render(dest: Path, width: int, height: int, *, frame: Path | None = None,
           theme: thumbnails.Theme | None = None, series: str = "",
           character: Path | None = None, faces: str = "left", glow: str = "",
           labels: list[str] | None = None, font_path: Path | None = None) -> EndScreen:
    theme = theme or thumbnails.Theme()
    sx, sy = width / REF_W, height / REF_H
    s = min(sx, sy)
    canvas = background(frame, width, height, theme)
    d = ImageDraw.Draw(canvas)
    slots = [_scale_box(b, sx, sy) for b in SLOTS]
    sub = _scale_box(SUBSCRIBE, sx, sy)
    texts = [_label_text(l, series) for l in (labels or DEFAULT_LABELS)][:len(slots)]
    accent = thumbnails._hex(theme.accent)
    stroke = thumbnails._hex(theme.stroke)

    # Video slots: dark panel with a white border (the YouTube element covers the middle).
    # Same text size for every label: fit each, then refit all to the smallest height.
    fitted = [thumbnails._fit_text(t, font_path, int(w * 0.95), int(64 * s) + 40, theme, "", one_line=True)
              for (_, _, w, _), t in zip(slots, texts)]
    common_h = min((f.height for f in fitted), default=0)
    for (x, y, w, h), text in zip(slots, texts):
        r = int(18 * s)
        d.rounded_rectangle((x - int(8 * s), y - int(8 * s), x + w + int(8 * s), y + h + int(8 * s)),
                            radius=r + int(6 * s), fill=(255, 255, 255, 235))
        d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=(14, 14, 18, 255))
        lab = thumbnails._fit_text(text, font_path, int(w * 0.95), common_h, theme, "", one_line=True)
        canvas.alpha_composite(lab, (x - 18, y - lab.height + int(12 * s)))

    # Subscribe circle: ring in the accent colour.
    cx, cy, cw, ch = sub
    ring = int(10 * s)
    d.ellipse((cx - ring * 2, cy - ring * 2, cx + cw + ring * 2, cy + ch + ring * 2), fill=stroke)
    d.ellipse((cx - ring, cy - ring, cx + cw + ring, cy + ch + ring), fill=accent)
    d.ellipse((cx, cy, cx + cw, cy + ch), fill=(14, 14, 18, 255))

    # Character on the right, looking toward the slots (left).
    if character and Path(character).exists():
        char = thumbnails._rgba(Path(character))
        gap = int(36 * s)                      # keep the pointing hand off the subscribe circle
        area_w = width - (cx + cw + gap)
        f = min(height * 0.94 / char.height, area_w / char.width)
        char = thumbnails._resize(char, (max(1, int(char.width * f)), max(1, int(char.height * f))))
        if faces == "right":
            char = ImageOps.mirror(char)
        sticker = thumbnails._outlined(char, max(4, int(10 * s)))
        gpad = 0
        if glow or theme.character_glow:
            before = sticker.width
            sticker = thumbnails._glow(sticker, theme.character_glow or glow)
            gpad = (sticker.width - before) // 2
        x = max(width - sticker.width + gpad + int(10 * s), cx + cw + gap - gpad)
        y = height - sticker.height + gpad + int(40 * s)
        canvas.alpha_composite(sticker, (x, y))

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dest)
    return EndScreen(dest, slots, sub, texts)


def studio_steps(es: EndScreen, seconds: float) -> list[str]:
    """What to do in YouTube Studio so the elements line up with the drawn spots."""
    return [
        f"The last {seconds:.0f}s of the video is the end screen.",
        "YouTube Studio > Content > the video > Editor > End screen.",
        "First video only: add Video (Most recent upload) on the top box, Video (Best for viewer) or "
        "your series Playlist on the bottom box, and Subscribe on the circle. Drag each onto its spot.",
        "Every video after that: End screen > Import from video > pick the last one. Done.",
    ]
