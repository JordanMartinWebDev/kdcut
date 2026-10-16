"""Showcase thumbnails: the build or the view is the hook, the series title is the text.

Three layouts, modelled on the creator's own best thumbnails:
  vista    - one beautiful screenshot, full frame, series title block in a top corner
  hero     - the character (big, cropped at the legs, white outline) on one side with soft
             light rays behind him, the build on the other side, title block top opposite
  collage  - 3-5 screenshots in a grid (big centre tile), title block bottom centre

No icons, no glow, no hype words by default. Bright, saturated screenshots (no dark gradient).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from kdcut import thumbnails as T

W, H = T.W, T.H
LAYOUTS = {
    "vista": "one screenshot fills the frame; series title in a top corner; no character. "
             "Best for a beautiful view, a finished build seen from afar, or a new world.",
    "hero": "your character big on one side (cropped at the legs) with light rays behind, the "
            "build/subject on the other side. Best when one build or discovery is the episode.",
    "collage": "3-5 screenshots in a grid, title at the bottom. Best for recap or 'everything I "
               "built' episodes. Needs at least 3 screenshots.",
}
LABEL_COLOR = "#36D3FF"     # the cyan 'SEASON 2' line from the creator's thumbnails


@dataclass
class Shot:
    path: Path
    focus: tuple[float, float] = (0.5, 0.5)     # the subject, 0-1 in the screenshot
    zoom: float = 1.0                            # >1 crops in so the subject is bigger


@dataclass
class Showcase:
    layout: str = "vista"
    shots: list[Shot] = field(default_factory=list)
    character: str | None = None
    character_side: str = "left"                 # hero: side the character stands on
    title_corner: str = "top-left"               # vista: where the title block goes
    text: str = ""                               # optional short hook line under the title
    why: str = ""


# --------------------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------------------

def _grade(im: Image.Image, theme: T.Theme) -> Image.Image:
    """Bright and punchy, like a shader screenshot: no darkening, just colour and crispness."""
    im = ImageEnhance.Color(im).enhance(min(theme.saturation, 1.3))
    im = ImageEnhance.Contrast(im).enhance(min(theme.contrast, 1.1))
    return ImageEnhance.Sharpness(im).enhance(1.3)


def _frame(shot: Shot, size: tuple[int, int], at: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Crop the screenshot to `size` so the subject (shot.focus) lands at `at` in the frame."""
    src = Image.open(shot.path).convert("RGB")
    sw, sh = src.size
    w, h = size
    scale = max(w / sw, h / sh) * max(1.0, min(float(shot.zoom or 1.0), 2.0))
    big = src.resize((max(w, int(sw * scale)), max(h, int(sh * scale))), Image.LANCZOS)
    fx, fy = shot.focus
    x0 = int(fx * big.width - at[0] * w)
    y0 = int(fy * big.height - at[1] * h)
    x0 = min(max(x0, 0), big.width - w)
    y0 = min(max(y0, 0), big.height - h)
    return big.crop((x0, y0, x0 + w, y0 + h))


def _label(text: str, height: int, font_path: Path | None, color: str, stroke: str) -> Image.Image:
    sw = max(3, height // 7)
    return T._word(text.upper(), height, font_path, T._hex(color), T._hex(stroke), sw)


def title_block(theme: T.Theme, episode, font_path: Path | None, max_w: int, max_h: int,
                align: str = "left") -> Image.Image | None:
    """Series logo with the episode line under it ('EPISODE 3' in cyan), as one unit."""
    parts: list[Image.Image] = []
    if theme.logo and Path(theme.logo).exists():
        parts.append(T._rgba(Path(theme.logo)))
    elif theme.name:
        parts.append(_label(theme.name, 160, font_path, theme.accent, theme.stroke))
    if episode not in (None, "") and theme.badge:
        fmt = theme.badge if "{n}" in theme.badge else "EPISODE {n}"
        if fmt.strip().upper() in ("EP {N}", "#{N}"):
            fmt = "EPISODE {n}"
        ref = parts[0].height if parts else 200
        parts.append(_label(fmt.replace("{n}", str(episode)), int(ref * 0.42), font_path,
                            LABEL_COLOR, theme.stroke))
    if not parts:
        return None
    # the episode line spans most of the logo's width, so the block reads as one shape
    if len(parts) == 2:
        target = int(parts[0].width * 0.62)
        f = min(target / parts[1].width, parts[0].height * 0.5 / parts[1].height)
        parts[1] = T._resize(parts[1], (max(1, int(parts[1].width * f)), max(1, int(parts[1].height * f))))
    gap = -int(parts[0].height * 0.04)
    bw = max(p.width for p in parts)
    bh = sum(p.height for p in parts) + gap * (len(parts) - 1)
    block = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    y = 0
    for p in parts:
        x = {"left": 0, "right": bw - p.width}.get(align, (bw - p.width) // 2)
        block.alpha_composite(p, (x, y))
        y += p.height + gap
    f = min(max_w / block.width, max_h / block.height)
    block = T._resize(block, (max(1, int(block.width * f)), max(1, int(block.height * f))))
    return T._outlined(block, 0, shadow=12)


def _rays(size: tuple[int, int], center: tuple[int, int], n: int = 16, alpha: int = 70) -> Image.Image:
    """Soft white light rays fanning out from `center` (behind the character)."""
    w, h = size
    ss = 2
    m = Image.new("L", (w // ss, h // ss), 0)
    d = ImageDraw.Draw(m)
    cx, cy = center[0] / ss, center[1] / ss
    r = math.hypot(w, h) / ss
    step = 2 * math.pi / n
    for i in range(n):
        a0 = i * step
        a1 = a0 + step * 0.45
        d.polygon([(cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0)),
                   (cx + r * math.cos(a1), cy + r * math.sin(a1))], fill=alpha)
    # fade out with distance so the rays glow near him and vanish at the edges
    fade = Image.radial_gradient("L").resize((int(r * 2), int(r * 2)))
    fade = ImageOps.invert(fade).point(lambda v: int(v * 1.0))
    fl = Image.new("L", m.size, 0)
    fl.paste(fade, (int(cx - r), int(cy - r)))
    m = ImageChops.multiply(m, fl).filter(ImageFilter.GaussianBlur(3))
    m = m.resize(size, Image.LANCZOS)
    out = Image.new("RGBA", size, (255, 255, 255, 0))
    out.putalpha(m)
    return out


def _character(path: Path, faces: str, want: str, height: int) -> Image.Image:
    char = T._rgba(path)
    f = height / char.height
    char = T._resize(char, (max(1, int(char.width * f)), max(1, int(char.height * f))))
    if faces != "front" and faces != want:
        char = ImageOps.mirror(char)
    return T._outlined(char, 12, shadow=16)


def _hook(text: str, font_path: Path | None, theme: T.Theme, max_w: int, max_h: int) -> Image.Image:
    return T._fit_text(text.upper(), font_path, max_w, max_h, theme, "", one_line=len(text) <= 18)


# --------------------------------------------------------------------------------------
# Layouts
# --------------------------------------------------------------------------------------

def render(sc: Showcase, theme: T.Theme, *, font_path: Path | None = None,
           characters: dict[str, Path] | None = None, poses: dict | None = None,
           episode=None) -> Image.Image:
    layout = sc.layout if sc.layout in LAYOUTS else "vista"
    if layout == "collage" and len(sc.shots) < 3:
        layout = "hero" if sc.character else "vista"
    if layout == "hero" and not (sc.character and characters and sc.character in characters):
        layout = "vista"
    if layout == "collage":
        canvas = _collage(sc, theme)
    else:
        canvas = Image.new("RGBA", (W, H))
    title_max = (int(W * 0.46), int(H * 0.36))

    if layout == "vista":
        canvas = _grade(_frame(sc.shots[0], (W, H), (0.5, 0.55)), theme).convert("RGBA")
        corner = sc.title_corner if sc.title_corner in ("top-left", "top-right") else "top-left"
        tb = title_block(theme, episode, font_path, *title_max, align="left" if corner == "top-left" else "right")
        if tb:
            x = 18 if corner == "top-left" else W - tb.width - 18
            canvas.alpha_composite(tb, (x, 10))
            _place_hook(canvas, sc.text, font_path, theme, x, 10 + tb.height, tb.width, corner == "top-left")

    elif layout == "hero":
        left = sc.character_side != "right"
        # the subject sits in the open side of the frame
        # zoom in a little so there's room to slide the subject to the open side
        hs = Shot(sc.shots[0].path, sc.shots[0].focus, max(sc.shots[0].zoom, 1.25))
        canvas = _grade(_frame(hs, (W, H), (0.68 if left else 0.32, 0.58)), theme).convert("RGBA")
        info = (poses or {}).get(sc.character)
        faces = getattr(info, "faces", "left")
        sticker = _character(characters[sc.character], faces, "right" if left else "left", int(H * 1.12))
        x = -int(sticker.width * 0.10) if left else W - int(sticker.width * 0.90)
        y = int(H * 0.06)
        cx = x + sticker.width // 2
        canvas.alpha_composite(_rays((W, H), (cx, int(H * 0.42))))
        canvas.alpha_composite(sticker, (x, y))
        tb = title_block(theme, episode, font_path, int(W * 0.50), int(H * 0.34),
                         align="right" if left else "left")
        if tb:
            tx = W - tb.width - 18 if left else 18
            canvas.alpha_composite(tb, (tx, 10))
            _place_hook(canvas, sc.text, font_path, theme, tx, 10 + tb.height, tb.width, not left)

    else:  # collage: title bottom centre, over the big middle tile
        tb = title_block(theme, episode, font_path, int(W * 0.56), int(H * 0.34), align="center")
        if tb:
            canvas.alpha_composite(tb, ((W - tb.width) // 2, H - tb.height - 4))
    return canvas.convert("RGB")


def _place_hook(canvas, text, font_path, theme, x, y, width, left):
    if not text:
        return
    hk = _hook(text, font_path, theme, max(width, int(W * 0.40)), int(H * 0.16))
    hx = x if left else x + width - hk.width
    canvas.alpha_composite(hk, (max(0, hx), y - 6))


def _collage(sc: Showcase, theme: T.Theme) -> Image.Image:
    shots = sc.shots[:5]
    gut = 7
    canvas = Image.new("RGBA", (W, H), (16, 16, 18, 255))
    side = shots[1:]
    nl = (len(side) + 1) // 2           # tiles in the left column
    nr = len(side) - nl
    col_w = int(W * 0.29)
    if nr == 0:                          # 2 tiles total + centre: centre takes the rest
        col_w = int(W * 0.36)
    cx0 = col_w + gut if nl else 0
    cx1 = W - (col_w + gut) if nr else W
    centre = _grade(_frame(shots[0], (cx1 - cx0, H)), theme)
    canvas.paste(centre, (cx0, 0))

    def column(tiles, x):
        th = (H - gut * (len(tiles) - 1)) // len(tiles)
        for i, s in enumerate(tiles):
            canvas.paste(_grade(_frame(s, (col_w, th)), theme), (x, i * (th + gut)))

    if nl:
        column(side[:nl], 0)
    if nr:
        column(side[nl:], W - col_w)
    return canvas


# --------------------------------------------------------------------------------------
# Whole step (AI picks layout, shots, pose; no hype words)
# --------------------------------------------------------------------------------------

SYSTEM = """You design YouTube thumbnails for a Minecraft survival let's-play about building a
kingdom. The channel's best thumbnails look like this:
- the screenshot IS the hook: a beautiful view or an impressive build, bright and saturated,
  big in the frame
- the only text is the series title block (logo + episode number), added automatically
- the creator's character appears only when it helps: big, on one side, next to the build
- no icons, no arrows, no hype words, no clickbait
Pick, for each concept:
- layout: vista | hero | collage (descriptions below). Make the concepts different from each other.
- backgrounds: screenshot numbers. vista/hero use the first; collage uses 3-5, best one first
  (it becomes the big centre tile).
- focus: where the subject is in the FIRST screenshot (0-1), so the crop keeps it big and clear.
- zoom: 1.0 to 1.6. Above 1 when the subject is small in the screenshot.
- character: a pose name for hero (matching the mood), else null.
- text: leave EMPTY unless the episode truly needs 1-3 words to make sense (e.g. "THE CASTLE").
  Never questions, never '!!', never hype.
"""


def concept_schema(want_theme: bool) -> dict:
    concept = {"type": "object", "properties": {
        "layout": {"type": "string", "enum": list(LAYOUTS),
                   "description": "; ".join(f"{k}: {v}" for k, v in LAYOUTS.items())},
        "backgrounds": {"type": "array", "items": {"type": "integer", "minimum": 1},
                        "minItems": 1, "maxItems": 5},
        "focus": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}},
        "zoom": {"type": "number", "minimum": 1.0, "maximum": 1.6},
        "character": {"type": ["string", "null"]},
        "character_side": {"type": "string", "enum": ["left", "right"],
                           "description": "hero: the side away from the subject"},
        "title_corner": {"type": "string", "enum": ["top-left", "top-right"],
                         "description": "vista: the emptier top corner (usually sky)"},
        "text": {"type": "string", "description": "Usually empty. At most 3 words."},
        "why": {"type": "string"},
    }, "required": ["layout", "backgrounds"]}
    props = {"concepts": {"type": "array", "items": concept, "minItems": 1, "maxItems": 3}}
    required = ["concepts"]
    if want_theme:
        props["theme"] = {"type": "object", "properties": {
            "accent": {"type": "string", "description": "#RRGGBB"},
            "font": {"type": "string", "description": "Exact name from the font list."},
        }, "required": ["accent"]}
        required.append("theme")
    return {"type": "object", "properties": props, "required": required}


def _clamp01(v, default=0.5) -> float:
    try:
        return min(max(float(v), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


def default_concepts(screenshots: list[Path], chars: dict[str, Path]) -> list[Showcase]:
    """Without the AI: a vista, a hero and (with 3+ screenshots) a collage."""
    shots = [Shot(p) for p in screenshots]
    out = [Showcase("vista", shots[:1], title_corner="top-left")]
    pose = next((n for n in ("point_side", "presenting", "excited", "wave") if n in chars), next(iter(chars), None))
    if pose:
        out.append(Showcase("hero", [shots[1 % len(shots)]], character=pose, character_side="left"))
    if len(shots) >= 3:
        out.append(Showcase("collage", shots[:5]))
    elif len(out) < 3:
        out.append(Showcase("vista", [shots[-1]], title_corner="top-right"))
    return out


def make(cfg: dict, video: dict, screenshots: list[Path], out_dir: Path, *, titles: list[str],
         text_ideas: list[str], index, use_ai: bool, caller=None, log=print) -> list[Path]:
    from kdcut import llm
    caller = caller or llm.call_tool
    tdir = out_dir / "thumbnails"
    tdir.mkdir(parents=True, exist_ok=True)
    series = video.get("series") or ""
    tpath = T.theme_path(cfg, series)
    theme = T.Theme.load(tpath)
    want_theme = theme is None or not theme.designed
    fonts = T.font_assets(index)
    chars = {p.stem: p for p in T.list_characters(cfg)}
    poses, _style = T.pose_library(cfg)
    shots_all = screenshots[:5]
    concepts: list[Showcase] = []
    if use_ai:
        try:
            info = [f"Video: {video.get('title', '')}", f"What happens: {video.get('topic', '')}"]
            if series:
                info.append(f"Series: {series}" + (f", episode {video['episode']}" if video.get("episode") else ""))
            if titles:
                info.append("Title ideas: " + " | ".join(titles[:5]))
            if chars:
                info.append("Character poses:\n" + "\n".join(
                    f"- {n}: {poses[n].mood}" if n in poses and poses[n].mood else f"- {n}" for n in chars))
            else:
                info.append("Character poses: (none: use null, and don't pick hero)")
            info.append(f"{len(shots_all)} screenshot(s)." + ("" if len(shots_all) >= 3 else " Collage needs 3+."))
            if want_theme:
                info.append("No style exists yet for this series: pick an accent colour that pops on Minecraft "
                            "greens/blues, and a chunky readable font from: "
                            + (", ".join(sorted(fonts)) if fonts else "(none: use 'default')"))
            content: list[dict] = [{"type": "text", "text": "\n".join(info)}]
            for i, shot in enumerate(shots_all, 1):
                content.append({"type": "text", "text": f"Screenshot {i}:"})
                content.append(llm.image_block(shot, max_side=768))
            content.append({"type": "text", "text": f"Design {cfg['thumbnails'].get('variants', 3)} "
                                                    "thumbnail concepts by calling the tool."})
            log("Designing thumbnails")
            res = caller(cfg, SYSTEM, content, "design_thumbnails", "Submit thumbnail concepts.",
                         concept_schema(want_theme))
            if want_theme and res.get("theme"):
                t = res["theme"]
                theme = theme or T.Theme(name=series or cfg["channel"].get("name", ""))
                theme.accent = t.get("accent", theme.accent)
                if t.get("font") in fonts:
                    theme.font = t["font"]
                theme.designed = True
                theme.save(tpath)
                log(f"Designed a thumbnail style for '{theme.name}' (reused for every episode)")
            for c in res.get("concepts", []):
                idx = [i for i in (c.get("backgrounds") or [1]) if isinstance(i, int) and 1 <= i <= len(shots_all)]
                if not idx:
                    idx = [1]
                f = c.get("focus") if isinstance(c.get("focus"), dict) else {}
                zoom = min(max(float(c.get("zoom") or 1.0), 1.0), 1.6)
                shots = [Shot(shots_all[i - 1]) for i in idx]
                shots[0].focus = (_clamp01(f.get("x")), _clamp01(f.get("y")))
                shots[0].zoom = zoom
                char = c.get("character") if c.get("character") in chars else None
                concepts.append(Showcase(
                    layout=c.get("layout") if c.get("layout") in LAYOUTS else "vista", shots=shots,
                    character=char, character_side=c.get("character_side", "left"),
                    title_corner=c.get("title_corner", "top-left"),
                    text=" ".join(str(c.get("text") or "").split()[:3]), why=str(c.get("why", ""))))
        except Exception as exc:  # thumbnails are a bonus; fall back to the defaults
            log(f"AI thumbnail design skipped: {exc}")
    if theme is None:
        theme = T.Theme(name=series or cfg["channel"].get("name", ""), badge="EPISODE {n}",
                        font=next((n for n in fonts if __import__("re").search(
                            r"luckiest|black|bold|bangers|minecraft ten", n, __import__("re").I)), ""))
    if not tpath.exists():
        theme.save(tpath)
    if not concepts:
        concepts = default_concepts(shots_all, chars)

    font_path = fonts.get(theme.font)
    existing = [int(p.stem.split("_")[1]) for p in tdir.glob("thumbnail_*.jpg") if p.stem.split("_")[1].isdigit()]
    start = max(existing, default=0)
    made: list[Path] = []
    for n, sc in enumerate(concepts[: int(cfg["thumbnails"].get("variants", 3))], start + 1):
        img = render(sc, theme, font_path=font_path, characters=chars, poses=poses, episode=video.get("episode"))
        dest = tdir / f"thumbnail_{n}.jpg"
        img.save(dest, "JPEG", quality=92, optimize=True)
        made.append(dest)
    import json
    from dataclasses import asdict
    (tdir / "concepts.json").write_text(json.dumps(
        [{**asdict(c), "shots": [{"path": str(s.path), "focus": s.focus, "zoom": s.zoom} for s in c.shots]}
         for c in concepts], indent=2), encoding="utf-8")
    return made
