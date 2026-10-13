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
    why: str = ""


SYSTEM = """You design YouTube thumbnails for a Minecraft let's-play channel.
Rules that make thumbnails get clicked:
- 2-4 words, max ~20 characters. Never repeat the title; add curiosity or emotion
  ("IT FOLLOWED ME", "1 HEART LEFT", "FINALLY!!"). Highlight ONE word in the accent colour.
- One clear focal point. Text on one side, the character (if any) on the other side.
- Keep the bottom-right corner empty (YouTube shows the video length there).
- Icons only if they tell the story at a glance (the item found, the mob fought). Max 2.
- Use a ring + arrow only when the screenshot has a specific thing worth pointing at.
- The character pose should match the emotion (shocked, celebrating, scared, pointing).
- Make the 3 concepts genuinely different (different words, sides or emotion).
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
        "why": {"type": "string"},
    }, "required": ["background", "text", "text_side"]}
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
              highlight: str) -> Image.Image:
    lines = _split(text)
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


def render(concept: Concept, background: Path, theme: Theme, *, font_path: Path | None = None,
           icons: dict[str, Path] | None = None, characters: dict[str, Path] | None = None,
           episode: str | int | None = None) -> Image.Image:
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
    left = concept.text_side != "right"

    # Darken behind the text and around the edges so everything on top pops.
    grad = Image.linear_gradient("L").rotate(90 if left else -90).resize((W, H))
    grad = grad.point(lambda v: int(max(0, v - 90) * 0.85))
    canvas.alpha_composite(Image.merge("RGBA", (*[Image.new("L", (W, H), 0)] * 3, grad)))
    if theme.vignette > 0:
        vig = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(max(0, v - 120) * theme.vignette * 1.6))
        canvas.alpha_composite(Image.merge("RGBA", (*[Image.new("L", (W, H), 0)] * 3, vig)))

    # Character on the side opposite the text.
    char_box, char_layer = None, None
    if concept.character and characters and concept.character in characters:
        char = _rgba(characters[concept.character])
        f = min(H * 0.96 / char.height, W * 0.46 / char.width)   # wide poses stay on their side
        char = _resize(char, (max(1, int(char.width * f)), max(1, int(char.height * f))))
        if not left:
            char = ImageOps.mirror(char)
        sticker = _outlined(char, 10)
        x = W - sticker.width + 40 if left else -40
        y = H - sticker.height + 60
        char_box = (x + 30, y + 30, x + sticker.width - 30, H)
        char_layer = (sticker, (x, y))

    # Ring + arrow pointing at the key thing in the screenshot (focus is in screenshot space;
    # after the crop it lands roughly where `centering` put it).
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
            if concept.ring:
                d = ImageDraw.Draw(canvas)
                r = 95
                d.ellipse([fx - r - 9, fy - r - 9, fx + r + 9, fy + r + 9], outline=(255, 255, 255, 255), width=30)
                d.ellipse([fx - r, fy - r, fx + r, fy + r], outline=(235, 40, 40, 255), width=14)

    if char_layer:
        canvas.alpha_composite(*char_layer)

    # The words.
    text = concept.text.upper()
    has_char = bool(concept.character and characters and concept.character in characters)
    max_w = int(W * (0.56 if has_char else 0.62))
    wants_icons = bool(icons and any(n in icons for n in concept.icons))
    layer = _fit_text(text, font_path, max_w, int(H * (0.36 if wants_icons else 0.46)), theme,
                      concept.highlight)
    if theme.tilt:
        layer = layer.rotate(theme.tilt, expand=True, resample=Image.BICUBIC)
    tx = 30 if left else W - layer.width - 30
    ty = 34
    canvas.alpha_composite(layer, (tx, ty))

    # Icons under the text.
    used = [icons[n] for n in concept.icons if icons and n in icons][:2]
    cursor = tx + 30 if left else tx + layer.width - 30
    iy = ty + layer.height + 4
    logo_img = None
    if theme.logo and Path(theme.logo).exists():
        logo_img = _rgba(Path(theme.logo))
        f = min(W * 0.34 / logo_img.width, H * 0.2 / logo_img.height)
        logo_img = _resize(logo_img, (max(1, int(logo_img.width * f)), max(1, int(logo_img.height * f))))
        logo_img = _outlined(logo_img, 0, shadow=10)
    reserved = max(150 if theme.badge and episode not in (None, "") else 30,
                   (logo_img.height + 10) if logo_img is not None else 0)
    bottom_limit = H - reserved
    room = bottom_limit - iy - 80          # 80 = outline, shadow and rotation padding
    target = min(230, room)
    if target < 110:                        # no space under the text: skip icons
        used = []
    for k, path in enumerate(used):
        icon = _rgba(path)
        f = target / max(icon.size)
        icon = _resize(icon, (max(1, int(icon.width * f)), max(1, int(icon.height * f))),
                       Image.NEAREST if max(icon.size) <= 256 else Image.LANCZOS)
        icon = _outlined(icon.rotate(-10 if k == 0 else 12, expand=True, resample=Image.BICUBIC), 7)
        if not left:
            cursor -= icon.width
        y = min(iy, bottom_limit - icon.height)
        canvas.alpha_composite(icon, (int(cursor), int(y)))
        cursor += (icon.width - 30) if left else 30

    if focus_xy and concept.arrow:
        start = (tx + layer.width * (0.85 if left else 0.15), ty + layer.height * 0.8)
        dx, dy = focus_xy[0] - start[0], focus_xy[1] - start[1]
        dist = math.hypot(dx, dy) or 1
        end = (focus_xy[0] - dx / dist * 115, focus_xy[1] - dy / dist * 115)
        if dist > 220:
            _arrow(ImageDraw.Draw(canvas), start, end, (235, 40, 40, 255))

    # Series branding: edge bar, logo and episode badge (bottom-left, away from the timestamp).
    if theme.edge_bar:
        bar = Image.new("RGBA", (14, H), _hex(theme.primary))
        canvas.alpha_composite(bar, (0 if left else W - 14, 0))
    badge_x = 26
    if logo_img is not None:
        canvas.alpha_composite(logo_img, (14, H - logo_img.height - 6))
        badge_x = 14 + logo_img.width - 40
    if theme.badge and episode not in (None, ""):
        label = theme.badge.replace("{n}", str(episode))
        f = _font(font_path, 64)
        box = f.getbbox(label, stroke_width=4)
        bw, bh = box[2] - box[0] + 50, box[3] - box[1] + 30
        badge = Image.new("RGBA", (bw + 16, bh + 16), (0, 0, 0, 0))
        bd = ImageDraw.Draw(badge)
        bd.rounded_rectangle([8, 8, bw + 8, bh + 8], radius=18, fill=_hex(theme.primary),
                             outline=(255, 255, 255, 255), width=6)
        bd.text((33 - box[0], 23 - box[1]), label, font=f, fill=(255, 255, 255, 255),
                stroke_width=4, stroke_fill=_hex(theme.stroke))
        badge = badge.rotate(theme.tilt, expand=True, resample=Image.BICUBIC)
        canvas.alpha_composite(badge, (badge_x, H - badge.height - 22))
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
                    ring=bool(c.get("ring")), arrow=bool(c.get("arrow")), why=str(c.get("why", ""))))
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
