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
# Inside YouTube's safe area: the editor won't place elements in the outer ~10% of the frame
# (the bottom one stopped about 85% of the way down), and a video element is at most about
# 613 px wide at 1080p. Slightly smaller boxes leave room to drag them into place.
SLOTS = [(192, 170, 576, 324), (192, 556, 576, 324)]
SUBSCRIBE = (850, 376, 298, 298)       # circle bounding box: YouTube's subscribe circle
DEFAULT_LABELS = ["WATCH NEXT", "MORE {series}"]


@dataclass
class EndScreen:
    image: Path
    slots: list[tuple[int, int, int, int]]
    subscribe: tuple[int, int, int, int]
    labels: list[str]
    video: Path | None = None       # the animated version, when made
    layers: "Layers | None" = None


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


@dataclass
class Sprite:
    image: Image.Image          # RGBA
    x: int
    y: int

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.image.width / 2, self.y + self.image.height / 2


@dataclass
class Layers:
    """The end screen in pieces, so it can be drawn still or animated."""
    width: int
    height: int
    bg: Image.Image                     # blurred, darkened background (RGBA)
    sharp: Image.Image | None           # the episode's last frame, untouched (RGB)
    slots: list[Sprite]
    ring: Sprite                        # coloured ring around the subscribe circle
    disk: Sprite                        # the dark circle itself (YouTube's element covers it)
    character: Sprite | None
    accent: tuple[int, int, int]
    slot_boxes: list[tuple[int, int, int, int]]
    sub_box: tuple[int, int, int, int]
    labels: list[str]


def _cropped(layer: Image.Image) -> Sprite:
    box = layer.getbbox() or (0, 0, 1, 1)
    return Sprite(layer.crop(box), box[0], box[1])


def layers(width: int, height: int, *, frame: Path | None = None,
           theme: thumbnails.Theme | None = None, series: str = "",
           character: Path | None = None, faces: str = "left", glow: str = "",
           labels: list[str] | None = None, font_path: Path | None = None) -> Layers:
    theme = theme or thumbnails.Theme()
    sx, sy = width / REF_W, height / REF_H
    s = min(sx, sy)
    bg = background(frame, width, height, theme)
    sharp = None
    if frame and Path(frame).exists():
        sharp = ImageOps.fit(Image.open(frame).convert("RGB"), (width, height), Image.LANCZOS)
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
    slot_sprites = []
    for (x, y, w, h), text in zip(slots, texts):
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        r = int(18 * s)
        d.rounded_rectangle((x - int(8 * s), y - int(8 * s), x + w + int(8 * s), y + h + int(8 * s)),
                            radius=r + int(6 * s), fill=(255, 255, 255, 235))
        d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=(14, 14, 18, 255))
        lab = thumbnails._fit_text(text, font_path, int(w * 0.95), common_h, theme, "", one_line=True)
        layer.alpha_composite(lab, (max(x - 18, 0), max(y - lab.height + int(12 * s), 0)))
        slot_sprites.append(_cropped(layer))

    # Subscribe circle: ring in the accent colour, dark disk inside.
    cx, cy, cw, ch = sub
    ring = int(10 * s)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - ring * 2, cy - ring * 2, cx + cw + ring * 2, cy + ch + ring * 2), fill=stroke)
    d.ellipse((cx - ring, cy - ring, cx + cw + ring, cy + ch + ring), fill=accent)
    ring_sprite = _cropped(layer)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((cx, cy, cx + cw, cy + ch), fill=(14, 14, 18, 255))
    disk_sprite = _cropped(layer)

    # Character on the right, looking toward the slots (left).
    char_sprite = None
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
        char_sprite = Sprite(sticker, x, y)
    return Layers(width, height, bg, sharp, slot_sprites, ring_sprite, disk_sprite, char_sprite,
                  accent[:3], slots, sub, texts)


def compose(L: Layers) -> Image.Image:
    canvas = L.bg.copy()
    for sp in [*L.slots, L.ring, L.disk] + ([L.character] if L.character else []):
        canvas.alpha_composite(sp.image, (sp.x, sp.y))
    return canvas


def render(dest: Path, width: int, height: int, **kw) -> EndScreen:
    """The end screen as a still image (also the first look at it in review and the app)."""
    L = layers(width, height, **kw)
    dest.parent.mkdir(parents=True, exist_ok=True)
    compose(L).convert("RGB").save(dest)
    return EndScreen(dest, L.slot_boxes, L.sub_box, L.labels, layers=L)


# --------------------------------------------------------------------------------------
# Motion: the end screen as a clip instead of a still
# --------------------------------------------------------------------------------------
#
# 0.0-0.8 s  the episode's last frame softens into the blurred background (no hard cut)
# 0.3-0.9 s  the video boxes pop in one after the other, then the subscribe ring
# 0.2-1.0 s  the character slides in from the right
# after      everything holds its spot for YouTube's elements; the background drifts, the
#            character bobs, the ring pulses and a few pixels float up. Fades out at the end.

INTRO = 1.2


def _ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def _ease_back(t: float, k: float = 1.7) -> float:
    t = min(max(t, 0.0), 1.0) - 1
    return 1 + (k + 1) * t ** 3 + k * t ** 2


def _scaled(sp: Sprite, f: float, alpha: float = 1.0) -> tuple[Image.Image, int, int]:
    im = sp.image
    if abs(f - 1.0) > 1e-3:
        w, h = max(1, int(im.width * f)), max(1, int(im.height * f))
        im = im.resize((w, h), Image.BILINEAR)
    if alpha < 0.999:
        a = im.getchannel("A").point(lambda v, m=alpha: int(v * m))
        im = im.copy()
        im.putalpha(a)
    cx, cy = sp.center
    return im, int(round(cx - im.width / 2)), int(round(cy - im.height / 2))


class _Pixels:
    """A few Minecraft-ish square motes drifting up behind everything."""

    def __init__(self, width: int, height: int, color, n: int = 28, seed: int = 7):
        import random
        rnd = random.Random(seed)
        s = height / REF_H
        self.h = height
        self.motes = [(rnd.uniform(0, width), rnd.uniform(0, height), rnd.uniform(18, 46) * s,
                       int(rnd.choice([8, 10, 12, 16]) * s) or 2, rnd.uniform(0, 6.28),
                       rnd.choice([color, color, (255, 255, 255)]), rnd.randint(70, 150)) for _ in range(n)]

    def draw(self, canvas: Image.Image, t: float, fade: float) -> None:
        import math
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for x, y, speed, size, phase, col, alpha in self.motes:
            yy = (y - speed * t) % (self.h + 40) - 20
            xx = x + 14 * math.sin(t * 0.8 + phase)
            a = int(alpha * fade * (0.6 + 0.4 * math.sin(t * 1.7 + phase)))
            d.rectangle((xx, yy, xx + size, yy + size), fill=(*col[:3], max(a, 0)))
        canvas.alpha_composite(layer)


def frame_at(L: Layers, t: float, total: float, pixels: _Pixels | None = None) -> Image.Image:
    import math
    w, h = L.width, L.height
    # Background: slow push in, and a soft blend out of the episode's last frame.
    z = 1.0 + 0.06 * (t / max(total, 1e-6))
    bw, bh = int(w * z), int(h * z)
    bg = L.bg.resize((bw, bh), Image.BILINEAR).crop(((bw - w) // 2, (bh - h) // 2, (bw - w) // 2 + w,
                                                     (bh - h) // 2 + h))
    if L.sharp is not None and t < 0.8:
        bg = Image.blend(L.sharp.convert("RGBA"), bg, _ease_out(t / 0.8))
    canvas = bg
    if pixels is not None:
        pixels.draw(canvas, t, _ease_out((t - 0.6) / 1.0))
    # Video boxes pop in, one after the other, then stay exactly in place.
    for i, sp in enumerate(L.slots):
        k = (t - 0.3 - 0.15 * i) / 0.45
        if k <= 0:
            continue
        im, x, y = _scaled(sp, 0.8 + 0.2 * _ease_back(k), _ease_out(k * 1.6))
        canvas.paste(im, (x, y), im)
    # Subscribe: ring pops in, then pulses gently; the dark disk never moves.
    k = (t - 0.65) / 0.4
    if k > 0:
        pulse = 0.035 * (0.5 - 0.5 * math.cos(2 * math.pi * max(t - 1.2, 0) / 1.8)) if t > 1.2 else 0.0
        im, x, y = _scaled(L.ring, (0.6 + 0.4 * _ease_back(k)) * (1 + pulse), _ease_out(k * 1.5))
        canvas.paste(im, (x, y), im)
        im, x, y = _scaled(L.disk, 0.6 + 0.4 * _ease_back(k), _ease_out(k * 1.5))
        canvas.paste(im, (x, y), im)
    # Character slides in from the right, then bobs.
    if L.character is not None:
        sp = L.character
        k = _ease_out((t - 0.2) / 0.8)
        dx = int((1 - k) * (w - sp.x + 40))
        dy = int(round(6 * h / REF_H * math.sin(2 * math.pi * max(t - 1.0, 0) / 2.6))) if t > 1.0 else 0
        canvas.paste(sp.image, (sp.x + dx, sp.y + dy), sp.image)
    # Fade to black over the last half second.
    if total - t < 0.5:
        canvas = Image.blend(canvas, Image.new("RGBA", (w, h), (0, 0, 0, 255)), 1 - (total - t) / 0.5)
    return canvas


def animate(L: Layers, dest: Path, seconds: float, fps, log=None) -> Path:
    """Render the moving end screen to a video (silent; the music bed plays under it)."""
    import subprocess
    from fractions import Fraction
    from kdcut import media
    fps = Fraction(fps)
    frames = int(round(seconds * fps))
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{L.width}x{L.height}", "-r", str(fps), "-i", "-", "-frames:v", str(frames),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(dest)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=media.NO_WINDOW)
    pixels = _Pixels(L.width, L.height, L.accent)
    try:
        for n in range(frames):
            if log and n and n % int(fps * 5) == 0:
                log(f"Animating the end screen ({100 * n // frames}%)")
            proc.stdin.write(frame_at(L, n / float(fps), seconds, pixels).convert("RGB").tobytes())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.wait()
    if proc.returncode != 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise media.MediaError((err.strip().splitlines() or ["ffmpeg failed"])[0][:300])
    media._PROBE_CACHE.pop(dest, None)
    return dest


def studio_steps(es: EndScreen, seconds: float) -> list[str]:
    """What to do in YouTube Studio so the elements line up with the drawn spots."""
    return [
        f"The last {seconds:.0f}s of the video is the end screen.",
        "YouTube Studio > Content > the video > Editor > End screen.",
        "First video only: add Video (Most recent upload) on the top box, Video (Best for viewer) or "
        "your series Playlist on the bottom box, and Subscribe on the circle. Drag each onto its spot "
        "and drag a corner to shrink the video boxes until they sit inside the white borders.",
        "Every video after that: End screen > Import from video > pick the last one. Done.",
    ]
