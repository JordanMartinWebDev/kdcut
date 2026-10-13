"""Thumbnail maker.

You give it a screenshot (or a few). It builds 1280x720 thumbnails in a consistent style
per series: punchy 2-4 word text with a thick outline, your character (poses made with
Brazey's Emote Generator), one or two item icons from your packs, an episode badge, and
optionally a ring + arrow pointing at the key thing in the screenshot.

Style rules baked in (YouTube thumbnail best practices):
  * readable at phone size: few words, huge, high contrast, thick dark outline
  * one clear subject; text and character on opposite sides
  * nothing important in the bottom-right corner (YouTube puts the video length there)
  * punchier colours than the game (saturation/contrast boost, vignette)
  * the same font, colours and badge every episode, so the series is recognisable

The AI picks the words, layout, icons and pose for each video, and designs the series
theme the first time it sees a new series. Without the AI a sensible default is used.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

from kdcut import llm
from kdcut.assets import AssetIndex

W, H = 1280, 720
IMAGE_EXTS = {".png", ".webp", ".gif", ".jpg", ".jpeg"}


# --------------------------------------------------------------------------------------
# Theme (one per series)
# --------------------------------------------------------------------------------------

@dataclass
class Theme:
    name: str = "KDPlays"
    primary: str = "#3FA34D"      # badge and edge bar
    accent: str = "#FFD23F"       # highlighted word
    text: str = "#FFFFFF"
    stroke: str = "#101010"
    font: str = ""                # font file name from your packs
    badge: str = "EP {n}"         # "" for no badge
    tilt: float = -3.0
    saturation: float = 1.35
    contrast: float = 1.15
    vignette: float = 0.55
    edge_bar: bool = True
    logo: str = ""                # series logo PNG (transparent), shown bottom-left
    designed: bool = False        # False until the AI has picked colours and font

    @classmethod
    def load(cls, path: Path) -> "Theme | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: data[k] for k in asdict(cls()) if k in data})
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def theme_path(cfg: dict, series: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", (series or cfg["channel"].get("name") or "channel").lower()).strip("-")
    return Path(cfg["thumbnails"]["themes_dir"]).expanduser() / f"{slug or 'channel'}.json"


# --------------------------------------------------------------------------------------
# Concepts (what goes on each thumbnail)
# --------------------------------------------------------------------------------------

@dataclass
class Concept:
    text: str
    highlight: str = ""
    background: int = 1                 # 1-based index into the screenshots
    text_side: str = "left"
    icons: list[str] = field(default_factory=list)
    character: str | None = None
    focus: dict | None = None           # {"x": 0-1, "y": 0-1} in the screenshot
    ring: bool = False
    arrow: bool = False
    layout: str = "side"                # side | top | bottom
    brand_corner: str = ""              # top-left | top-right | bottom-left | bottom-right
    why: str = ""


LAYOUTS = {
    "side": "words in the top corner on one side, character big on the other side",
    "top": "words in one wide banner across the top, character smaller in a bottom corner, "
           "icons on the open side",
    "bottom": "words low on one side, character on the other side, icons above the words",
}


SYSTEM = """You design YouTube thumbnails for a Minecraft let's-play channel.
Rules that make thumbnails get clicked:
- 2-4 words, max ~20 characters. Never repeat the title; add curiosity or emotion
  ("IT FOLLOWED ME", "1 HEART LEFT", "FINALLY!!"). Highlight ONE word in the accent colour.
- One clear focal point. Text on one side, the character (if any) on the other side.
- Keep the bottom-right corner empty (YouTube shows the video length there).
- Icons only if they tell the story at a glance (the item found, the mob fought). Max 2.
- Use a ring + arrow only when the screenshot has a specific thing worth pointing at.
- The character pose should match the emotion (shocked, celebrating, scared, pointing).
- The series style (colours, font, logo, episode tab) stays the same every video, but the
  composition should change from video to video. Pick a layout per concept and make the
  3 concepts genuinely different (layout, side, words, emotion).
- Put the logo + episode tab (brand_corner) where it won't cover the character, the words or
  the focus point. Never the bottom-right (YouTube's timestamp).
"""

THEME_RULES = """No style exists yet for this series, so also design one ("theme"). It is reused
for every future episode so the series is recognisable. Pick bold, high-contrast colours that
pop against Minecraft's greens and browns, and a chunky, readable font from the list (not a
script or thin font)."""


def concept_schema(want_theme: bool) -> dict:
    concept = {"type": "object", "properties": {
        "background": {"type": "integer", "minimum": 1},
        "text": {"type": "string"},
        "highlight": {"type": "string", "description": "One word from text to colour."},
        "text_side": {"type": "string", "enum": ["left", "right"],
                      "description": "Side with less going on in the screenshot."},
        "icons": {"type": "array", "items": {"type": "string"}, "maxItems": 2,
                  "description": "Exact names from the icon list."},
        "character": {"type": ["string", "null"], "description": "Exact pose name, or null."},
        "focus": {"type": ["object", "null"], "properties": {
            "x": {"type": "number"}, "y": {"type": "number"}},
            "description": "Most interesting point in the screenshot, 0-1 coordinates."},
        "ring": {"type": "boolean"},
        "arrow": {"type": "boolean"},
        "layout": {"type": "string", "enum": list(LAYOUTS),
                   "description": "; ".join(f"{k}: {v}" for k, v in LAYOUTS.items())},
        "brand_corner": {"type": "string", "enum": ["top-left", "top-right", "bottom-left"]},
        "why": {"type": "string"},
    }, "required": ["background", "text", "text_side", "layout"]}
    props = {"concepts": {"type": "array", "items": concept, "minItems": 1, "maxItems": 3}}
    required = ["concepts"]
    if want_theme:
        props["theme"] = {"type": "object", "properties": {
            "primary": {"type": "string", "description": "#RRGGBB"},
            "accent": {"type": "string", "description": "#RRGGBB"},
            "font": {"type": "string", "description": "Exact name from the font list."},
            "badge": {"type": "string", "description": "e.g. 'EP {n}' or '#{n}' or ''."},
            "tilt": {"type": "number", "minimum": -6, "maximum": 6},
        }, "required": ["primary", "accent", "font"]}
        required.append("theme")
    return {"type": "object", "properties": props, "required": required}


# --------------------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------------------

def list_characters(cfg: dict) -> list[Path]:
    d = (cfg.get("thumbnails", {}).get("characters_dir") or "").strip()
    if not d or not Path(d).expanduser().is_dir():
        return []
    return sorted(p for p in Path(d).expanduser().rglob("*.png"))


def font_assets(index: AssetIndex | None) -> dict[str, Path]:
    if not index:
        return {}
    out: dict[str, Path] = {}
    for a in index.assets:
        if a.category == "font":
            out.setdefault(Path(a.path).stem, Path(a.path))
    return out


def icon_candidates(index: AssetIndex | None, words: list[str], limit: int = 40) -> dict[str, Path]:
    """Still images from the packs that match the video's words."""
    if not index:
        return {}
    out: dict[str, Path] = {}
    seen_q = set()
    for w in words:
        w = w.strip().lower()
        if len(w) < 3 or w in seen_q:
            continue
        seen_q.add(w)
        for _, a in index.search(w, k=6):
            if Path(a.path).suffix.lower() in IMAGE_EXTS and a.category in ("image", "overlay"):
                out.setdefault(a.name, Path(a.path))
        if len(out) >= limit:
            break
    return dict(list(out.items())[:limit])


def _font(path: Path | None, size: int) -> ImageFont.FreeTypeFont:
    candidates = [path] if path else []
    candidates += [Path("C:/Windows/Fonts/impact.ttf"), Path("C:/Windows/Fonts/arialbd.ttf"),
                   Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]
    for c in candidates:
        if c and Path(c).exists():
            try:
                return ImageFont.truetype(str(c), size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _resize(im: Image.Image, size: tuple[int, int], resample=Image.LANCZOS) -> Image.Image:
    """Resize RGBA without the hidden colours of transparent pixels bleeding into edges."""
    return im.convert("RGBa").resize(size, resample).convert("RGBA")


def _rgba(path: Path) -> Image.Image:
    im = Image.open(path)
    if getattr(im, "is_animated", False):
        im.seek(min(im.n_frames // 2, im.n_frames - 1))
    im = im.convert("RGBA")
    box = im.getchannel("A").getbbox()
    return im.crop(box) if box else im


def _hex(c: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = c.lstrip("#")
    if len(c) != 6:
        c = "FFFFFF"
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------

def busier_side(img: Image.Image) -> str:
    edges = img.convert("L").resize((160, 90)).filter(ImageFilter.FIND_EDGES)
    left = ImageStat.Stat(edges.crop((0, 0, 80, 90))).sum[0]
    right = ImageStat.Stat(edges.crop((80, 0, 160, 90))).sum[0]
    return "left" if left > right else "right"


def _outlined(sprite: Image.Image, width: int, color=(255, 255, 255, 255), shadow: int = 14) -> Image.Image:
    """Sprite with a solid outline and soft drop shadow (the classic thumbnail 'sticker')."""
    pad = width + shadow * 2
    base = Image.new("RGBA", (sprite.width + pad * 2, sprite.height + pad * 2), (0, 0, 0, 0))
    alpha = Image.new("L", base.size, 0)
    alpha.paste(sprite.getchannel("A"), (pad, pad))
    grown = alpha.filter(ImageFilter.MaxFilter(width * 2 + 1)) if width > 0 else alpha
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 150))
    shadow_layer.putalpha(ImageChops.multiply(grown.filter(ImageFilter.GaussianBlur(shadow)),
                                              Image.new("L", base.size, 170)))
    base.alpha_composite(shadow_layer, (shadow // 2, shadow // 2))
    if width > 0:
        outline = Image.new("RGBA", base.size, color)
        outline.putalpha(grown)
        base.alpha_composite(outline)
    base.alpha_composite(sprite, (pad, pad))
    return base


def _text_layer(lines: list[str], font: ImageFont.FreeTypeFont, theme: Theme, highlight: str) -> Image.Image:
    stroke = max(4, font.size // 9)
    gap = int(font.size * 0.08)
    sizes = [font.getbbox(line, stroke_width=stroke) for line in lines]
    width = max(b[2] - b[0] for b in sizes) + stroke * 2 + 40
    height = sum(b[3] - b[1] for b in sizes) + gap * (len(lines) - 1) + stroke * 2 + 40
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    draw, sdraw = ImageDraw.Draw(layer), ImageDraw.Draw(shadow)
    y = 20
    hl = highlight.strip().upper()
    for line, box in zip(lines, sizes):
        x = 20 + (width - 40 - (box[2] - box[0])) // 2 - box[0]
        sdraw.text((x + 8, y - box[1] + 10), line, font=font, fill=(0, 0, 0, 200),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 200))
        for word in line.split(" "):
            fill = _hex(theme.accent) if hl and word.strip("!?.,").upper() == hl else _hex(theme.text)
            draw.text((x, y - box[1]), word, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=_hex(theme.stroke))
            x += font.getlength(word + " ")
        y += box[3] - box[1] + gap
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    shadow.alpha_composite(layer)
    return shadow


def _split(text: str) -> list[str]:
    words = text.split()
    if len(words) <= 1 or len(text) <= 10:
        return [text]
    best, best_diff = [text], 10 ** 9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(a) - len(b))
        if diff < best_diff:
            best, best_diff = [a, b], diff
    return best


def _fit_text(text: str, font_path: Path | None, max_w: int, max_h: int, theme: Theme,
              highlight: str, one_line: bool = False) -> Image.Image:
    lines = [text] if one_line else _split(text)
    lo, hi = 40, 260
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        layer = _text_layer(lines, _font(font_path, mid), theme, highlight)
        if layer.width <= max_w and layer.height <= max_h:
            best, lo = layer, mid + 1
        else:
            hi = mid - 1
    return best or _text_layer(lines, _font(font_path, 40), theme, highlight)


def _arrow(draw: ImageDraw.ImageDraw, start, end, color, width: int = 26):
    """Thick arrow with a white outline, from start to end (the tip)."""
    (x0, y0), (x1, y1) = start, end
    ang = math.atan2(y1 - y0, x1 - x0)
    head_len, head_w = width * 2.4, width * 1.9

    def shape(grow: float):
        bx, by = x1 - head_len * math.cos(ang), y1 - head_len * math.sin(ang)
        tip = (x1 + grow * math.cos(ang), y1 + grow * math.sin(ang))
        side = head_w + grow
        left = (bx + side * math.cos(ang + math.pi / 2), by + side * math.sin(ang + math.pi / 2))
        right = (bx + side * math.cos(ang - math.pi / 2), by + side * math.sin(ang - math.pi / 2))
        return (bx, by), [tip, left, right]

    for grow, col, w in ((8, (255, 255, 255, 255), width + 16), (0, color, width)):
        base, head = shape(grow)
        draw.line([(x0, y0), base], fill=col, width=w)
        draw.ellipse([x0 - w / 2, y0 - w / 2, x0 + w / 2, y0 + w / 2], fill=col)
        draw.polygon(head, fill=col)


def _brand(theme: Theme, episode, font_path: Path | None) -> Image.Image | None:
    """Series logo with the episode tab tucked underneath it, as one unit."""
    logo = None
    if theme.logo and Path(theme.logo).exists():
        logo = _rgba(Path(theme.logo))
        f = min(W * 0.30 / logo.width, H * 0.16 / logo.height)
        logo = _resize(logo, (max(1, int(logo.width * f)), max(1, int(logo.height * f))))
    tab = None
    if theme.badge and episode not in (None, ""):
        label = theme.badge.replace("{n}", str(episode))
        size = 40 if logo is not None else 58
        fnt = _font(font_path, size)
        box = fnt.getbbox(label, stroke_width=3)
        tw, th = box[2] - box[0], box[3] - box[1]
        pad_x, pad_y = int(size * 0.55), int(size * 0.3)
        tab = Image.new("RGBA", (tw + pad_x * 2 + 10, th + pad_y * 2 + 10), (0, 0, 0, 0))
        d = ImageDraw.Draw(tab)
        d.rounded_rectangle([5, 5, tab.width - 5, tab.height - 5], radius=int(size * 0.35),
                            fill=_hex(theme.primary), outline=(255, 255, 255, 255), width=5)
        d.text((5 + pad_x - box[0], 5 + pad_y - box[1]), label, font=fnt, fill=_hex(theme.text),
               stroke_width=3, stroke_fill=_hex(theme.stroke))
    if logo is None and tab is None:
        return None
    if logo is None:
        unit = tab
    elif tab is None:
        unit = logo
    else:
        gap = 2
        unit = Image.new("RGBA", (max(logo.width, tab.width), logo.height + gap + tab.height), (0, 0, 0, 0))
        unit.alpha_composite(logo, ((unit.width - logo.width) // 2, 0))
        unit.alpha_composite(tab, ((unit.width - tab.width) // 2, logo.height + gap))
    return _outlined(unit, 0, shadow=10)


Rect = tuple[int, int, int, int]
TIMESTAMP_ZONE: Rect = (W - 210, H - 95, W, H)   # where YouTube draws the video length


def _overlap(a: Rect, b: Rect) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0) * max(h, 0)


def _place(size: tuple[int, int], preferred: str, obstacles: list[Rect], margin: int = 22) -> tuple[int, int]:
    """Put a box in the preferred corner, or the corner that collides least."""
    w, h = size
    spots = {
        "top-left": (margin, margin),
        "top-right": (W - w - margin, margin),
        "bottom-left": (margin, H - h - margin),
        "bottom-right": (W - w - margin, H - h - 105),     # lifted above the timestamp
    }
    order = [preferred] + [k for k in spots if k != preferred]
    best, best_cost = spots[order[0]] if order[0] in spots else spots["bottom-left"], None
    for name in order:
        if name not in spots:
            continue
        x, y = spots[name]
        r = (x, y, x + w, y + h)
        cost = sum(_overlap(r, o) for o in obstacles) + _overlap(r, TIMESTAMP_ZONE) * 10
        if cost == 0:
            return x, y
        if best_cost is None or cost < best_cost:
            best, best_cost = (x, y), cost
    return best


def render(concept: Concept, background: Path, theme: Theme, *, font_path: Path | None = None,
           icons: dict[str, Path] | None = None, characters: dict[str, Path] | None = None,
           episode: str | int | None = None) -> Image.Image:
    layout = concept.layout if concept.layout in LAYOUTS else "side"
    src = Image.open(background).convert("RGB")
    centering = (0.5, 0.5)
    if concept.focus:
        centering = (min(max(float(concept.focus.get("x", .5)), 0), 1),
                     min(max(float(concept.focus.get("y", .5)), 0), 1))
    bg = ImageOps.fit(src, (W, H), Image.LANCZOS, centering=centering)
    bg = ImageEnhance.Color(bg).enhance(theme.saturation)
    bg = ImageEnhance.Contrast(bg).enhance(theme.contrast)
    bg = ImageEnhance.Sharpness(bg).enhance(1.4)
    canvas = bg.convert("RGBA")
    left = concept.text_side != "right"          # text side; the character goes opposite
    zeros = Image.new("L", (W, H), 0)

    # Darken behind the text so it pops.
    if layout == "side":
        shade = Image.linear_gradient("L").rotate(90 if left else -90).resize((W, H))
    elif layout == "top":
        shade = Image.linear_gradient("L").rotate(180).resize((W, H))
    else:
        shade = Image.linear_gradient("L").resize((W, H))
    shade = shade.point(lambda v: int(max(0, v - 90) * 0.85))
    canvas.alpha_composite(Image.merge("RGBA", (zeros, zeros, zeros, shade)))
    if theme.vignette > 0:
        vig = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(max(0, v - 120) * theme.vignette * 1.6))
        canvas.alpha_composite(Image.merge("RGBA", (zeros, zeros, zeros, vig)))

    obstacles: list[Rect] = []

    # Character, opposite the text. Smaller in the "top" layout so the words get the width.
    char_box, char_layer = None, None
    has_char = bool(concept.character and characters and concept.character in characters)
    if has_char:
        char = _rgba(characters[concept.character])
        height = 0.80 if layout == "top" else 0.96
        f = min(H * height / char.height, W * (0.40 if layout == "top" else 0.46) / char.width)
        char = _resize(char, (max(1, int(char.width * f)), max(1, int(char.height * f))))
        if not left:
            char = ImageOps.mirror(char)
        sticker = _outlined(char, 10)
        x = W - sticker.width + 40 if left else -40
        y = H - sticker.height + 60
        char_box = (x + 30, y + 30, x + sticker.width - 30, H)
        char_layer = (sticker, (x, y))
        obstacles.append(char_box)

    # Ring around the key thing in the screenshot (skipped if the character would hide it).
    focus_xy = None
    if concept.focus and (concept.ring or concept.arrow):
        sw, sh = src.size
        scale = max(W / sw, H / sh)
        cx = (sw * scale - W) * centering[0]
        cy = (sh * scale - H) * centering[1]
        fx = float(concept.focus.get("x", .5)) * sw * scale - cx
        fy = float(concept.focus.get("y", .5)) * sh * scale - cy
        covered = char_box and char_box[0] - 100 < fx < char_box[2] + 100 and fy > char_box[1] - 100
        if 60 < fx < W - 60 and 60 < fy < H - 60 and not covered:
            focus_xy = (fx, fy)
    if char_layer:
        canvas.alpha_composite(*char_layer)

    # The words.
    text = concept.text.upper()
    wants_icons = bool(icons and any(n in icons for n in concept.icons))
    if layout == "top":
        max_w, max_h = int(W * 0.92), int(H * 0.30)
    else:
        max_w = int(W * (0.56 if has_char else 0.62))
        max_h = int(H * (0.36 if wants_icons else 0.46))
    layer = _fit_text(text, font_path, max_w, max_h, theme, concept.highlight,
                      one_line=layout == "top" and len(text) <= 22)
    if theme.tilt:
        layer = layer.rotate(theme.tilt, expand=True, resample=Image.BICUBIC)
    if layout == "top":
        tx, ty = (W - layer.width) // 2, 18
    elif layout == "bottom":
        tx = 30 if left else W - layer.width - 30
        ty = H - layer.height - (40 if left else 120)       # stay above the timestamp on the right
    else:
        tx, ty = (30 if left else W - layer.width - 30), 34
    canvas.alpha_composite(layer, (tx, ty))
    text_box = (tx + 20, ty + 20, tx + layer.width - 20, ty + layer.height - 20)
    obstacles.append(text_box)

    if focus_xy:
        fx, fy = focus_xy
        ring_box = (int(fx - 110), int(fy - 110), int(fx + 110), int(fy + 110))
        if _overlap(ring_box, text_box) > 0:     # the words landed on it: don't point at it
            focus_xy = None
        else:
            if concept.ring:
                d = ImageDraw.Draw(canvas)
                r = 95
                d.ellipse([fx - r - 9, fy - r - 9, fx + r + 9, fy + r + 9], outline=(255, 255, 255, 255), width=30)
                d.ellipse([fx - r, fy - r, fx + r, fy + r], outline=(235, 40, 40, 255), width=14)
            obstacles.append(ring_box)

    # Icons: under the text (side), beside the character (top), above the text (bottom).
    used = [icons[n] for n in concept.icons if icons and n in icons][:2]
    if layout == "side":
        room = H - 150 - (ty + layer.height)
    elif layout == "top":
        room = H - (ty + layer.height) - 60
    else:
        room = ty - 60
    target = min(230, room - 80)
    if target < 110:
        used = []
    cursor = (tx + 30) if left else (tx + layer.width - 30)
    for k, path in enumerate(used):
        icon = _rgba(path)
        f = target / max(icon.size)
        icon = _resize(icon, (max(1, int(icon.width * f)), max(1, int(icon.height * f))),
                       Image.NEAREST if max(icon.size) <= 256 else Image.LANCZOS)
        icon = _outlined(icon.rotate(-10 if k == 0 else 12, expand=True, resample=Image.BICUBIC), 7)
        if layout == "top":
            ix = (W * 0.36 + k * (icon.width - 20)) if left else (W * 0.64 - icon.width - k * (icon.width - 20))
            iy = ty + layer.height + max(0, (room - icon.height) // 2)
        elif layout == "bottom":
            ix = cursor if left else cursor - icon.width
            iy = ty - icon.height + 30
        else:
            ix = cursor if left else cursor - icon.width
            iy = min(ty + layer.height + 4, H - 150 - icon.height)
        canvas.alpha_composite(icon, (int(ix), int(iy)))
        obstacles.append((int(ix) + 20, int(iy) + 20, int(ix) + icon.width - 20, int(iy) + icon.height - 20))
        if layout != "top":
            cursor += (icon.width - 30) if left else -(icon.width - 30)

    if focus_xy and concept.arrow:
        sx = text_box[2] - 40 if focus_xy[0] > (text_box[0] + text_box[2]) / 2 else text_box[0] + 40
        sy = text_box[3] - 10 if focus_xy[1] > text_box[3] else text_box[1] + 10
        dx, dy = focus_xy[0] - sx, focus_xy[1] - sy
        dist = math.hypot(dx, dy) or 1
        end = (focus_xy[0] - dx / dist * 115, focus_xy[1] - dy / dist * 115)
        if dist > 220:
            _arrow(ImageDraw.Draw(canvas), (sx, sy), end, (235, 40, 40, 255))

    # Series branding: edge bar, then logo + episode tab as one unit in a free corner.
    if theme.edge_bar:
        bar = Image.new("RGBA", (14, H), _hex(theme.primary))
        canvas.alpha_composite(bar, (0 if left else W - 14, 0))
    brand = _brand(theme, episode, font_path)
    if brand is not None:
        default = {"side": "bottom-left" if left else "top-left",
                   "top": "bottom-left" if left else "bottom-right",
                   "bottom": "top-left" if left else "top-right"}[layout]
        canvas.alpha_composite(brand, _place(brand.size, concept.brand_corner or default, obstacles))
    return canvas.convert("RGB")


# --------------------------------------------------------------------------------------
# Whole step
# --------------------------------------------------------------------------------------

def make(cfg: dict, video: dict, screenshots: list[Path], out_dir: Path, *, titles: list[str],
         text_ideas: list[str], index: AssetIndex | None, use_ai: bool, caller=llm.call_tool,
         log=print) -> list[Path]:
    """Create thumbnail variants in out_dir/thumbnails. Returns the image paths."""
    if not screenshots:
        return []
    tdir = out_dir / "thumbnails"
    tdir.mkdir(parents=True, exist_ok=True)
    series = video.get("series") or ""
    tpath = theme_path(cfg, series)
    theme = Theme.load(tpath)
    want_theme = theme is None or not theme.designed
    fonts = font_assets(index)
    chars = {p.stem: p for p in list_characters(cfg)}
    words = " ".join([video.get("topic", ""), series, *titles[:3], *text_ideas]).replace("'", " ").split()
    icons = icon_candidates(index, words)

    concepts: list[Concept] = []
    if use_ai:
        try:
            content: list[dict] = []
            info = [f"Video: {video.get('title', '')}", f"What happens: {video.get('topic', '')}"]
            if series:
                info.append(f"Series: {series}" + (f", episode {video['episode']}" if video.get("episode") else ""))
            if titles:
                info.append("Title ideas: " + " | ".join(titles[:5]))
            if text_ideas:
                info.append("Thumbnail text ideas: " + " | ".join(text_ideas))
            info.append("Icons available: " + (", ".join(icons) if icons else "(none)"))
            info.append("Character poses available: " + (", ".join(chars) if chars else "(none: use null)"))
            if want_theme:
                info.append(THEME_RULES)
                info.append("Fonts available: " + (", ".join(sorted(fonts)) if fonts else "(none: use 'default')"))
            else:
                info.append(f"Series style already set: accent {theme.accent}, primary {theme.primary}.")
            content.append({"type": "text", "text": "\n".join(info)})
            if want_theme and theme is not None and theme.logo and Path(theme.logo).exists():
                content.append({"type": "text", "text": "The series logo (match its colours):"})
                content.append(llm.image_block(Path(theme.logo), max_side=512))
            if want_theme and chars:
                content.append({"type": "text", "text": "The creator's character:"})
                content.append(llm.image_block(next(iter(chars.values())), max_side=384))
            for i, shot in enumerate(screenshots[:3], 1):
                content.append({"type": "text", "text": f"Screenshot {i}:"})
                content.append(llm.image_block(shot, max_side=768))
            content.append({"type": "text", "text": f"Design {cfg['thumbnails'].get('variants', 3)} "
                                                    "thumbnail concepts by calling the tool."})
            log("Designing thumbnails")
            res = caller(cfg, SYSTEM, content, "design_thumbnails", "Submit thumbnail concepts.",
                         concept_schema(want_theme))
            if want_theme and res.get("theme"):
                t = res["theme"]
                theme = Theme(name=series or cfg["channel"].get("name", ""),
                              primary=t.get("primary", Theme.primary), accent=t.get("accent", Theme.accent),
                              font=t.get("font", "") if t.get("font") in fonts else "",
                              badge=t.get("badge", "EP {n}"), tilt=float(t.get("tilt", -3)),
                              logo=theme.logo if theme else "", designed=True)
                theme.save(tpath)
                log(f"Designed a thumbnail style for '{theme.name}' (reused for every episode)")
            for c in res.get("concepts", []):
                concepts.append(Concept(
                    text=str(c.get("text", ""))[:40] or (text_ideas or ["WATCH THIS"])[0],
                    highlight=str(c.get("highlight") or ""),
                    background=int(c.get("background") or 1),
                    text_side=c.get("text_side", "left"),
                    icons=[i for i in (c.get("icons") or []) if i in icons],
                    character=c.get("character") if c.get("character") in chars else None,
                    focus=c.get("focus") if isinstance(c.get("focus"), dict) else None,
                    ring=bool(c.get("ring")), arrow=bool(c.get("arrow")),
                    layout=c.get("layout") if c.get("layout") in LAYOUTS else "side",
                    brand_corner=c.get("brand_corner") or "", why=str(c.get("why", ""))))
        except Exception as exc:  # thumbnails are a bonus; fall back to the plain layout
            log(f"AI thumbnail design skipped: {exc}")

    if theme is None:
        theme = Theme(name=series or cfg["channel"].get("name", ""),
                      font=next((n for n in fonts if re.search(r"luckiest|black|bold|bangers|minecraft ten", n, re.I)), ""))
    if not tpath.exists():
        theme.save(tpath)
        log(f"Saved the thumbnail style for '{theme.name}' (reused for every episode)")

    if not concepts:
        texts = [t for t in text_ideas if t] or [(titles or [video.get("title", "NEW EPISODE")])[0]]
        pose_names = list(chars)
        for i in range(min(int(cfg["thumbnails"].get("variants", 3)), max(len(texts), 1) * 2)):
            bgp = screenshots[i % len(screenshots)]
            with Image.open(bgp) as im:
                side = "right" if busier_side(im) == "left" else "left"
            concepts.append(Concept(text=texts[i % len(texts)], background=i % len(screenshots) + 1,
                                    layout=list(LAYOUTS)[i % len(LAYOUTS)],
                                    text_side=side if i % 2 == 0 else ("left" if side == "right" else "right"),
                                    character=pose_names[i % len(pose_names)] if pose_names else None))

    font_path = fonts.get(theme.font)
    made: list[Path] = []
    existing = [int(m.group(1)) for p in tdir.glob("thumbnail_*.jpg")
                if (m := re.match(r"thumbnail_(\d+)$", p.stem))]
    start = max(existing, default=0)
    for n, c in enumerate(concepts[: int(cfg["thumbnails"].get("variants", 3))], start + 1):
        bg = screenshots[min(max(c.background, 1), len(screenshots)) - 1]
        img = render(c, bg, theme, font_path=font_path, icons=icons, characters=chars,
                     episode=video.get("episode"))
        dest = tdir / f"thumbnail_{n}.jpg"
        img.save(dest, "JPEG", quality=92, optimize=True)   # under YouTube's 2 MB limit
        made.append(dest)
    (tdir / "concepts.json").write_text(json.dumps([asdict(c) for c in concepts], indent=2), encoding="utf-8")
    return made
