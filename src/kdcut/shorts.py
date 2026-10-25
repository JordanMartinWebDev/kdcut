"""Shorts: a vertical build Short from a timelapse, glamor shots of the finished build and a
short voiceover.

How a Short plays:

    timelapse (sped up to fit the voiceover)  ->  white flash  ->  glamor shots (the reveal)

Your voiceover plays from the start, tidied like the main edit (long pauses and anything after
"cut that" come out). Captions are big, bubbly and word-by-word, with the word you're saying
popping in the highlight colour; an optional title sits at the top the whole time. Music from
your library plays under it and dips while you talk.

The result is a finished 1080x1920 MP4 (no Resolve step) in the video's `short` folder, with
`short_kit.md` next to it (titles, description, hashtags).

Command-line projects put the files in `short/`: audio is the voiceover, videos with glamor,
reveal, showcase, cinematic, flyover or final in the name are glamor shots, and every other
video is a timelapse.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from kdcut import media

W, H = 1080, 1920
LEAD = 0.4          # seconds of picture before the voiceover starts
TAIL = 1.2          # after the last word, so the reveal can breathe
FLASH = 0.3         # white flash into the reveal
MIN_SPEED = 0.5     # how far a timelapse may be slowed down to fill time


GLAMOR_NAME = re.compile(r"glam|reveal|showcase|cinematic|fly ?over|fly ?by|beauty|final|finished|tour")
BUBBLY = ["luckiest guy", "lilita", "titan one", "bangers", "komika", "burbank", "chewy", "fredoka",
          "baloo", "paytone", "bubble", "bold font", "bungee", "rubik", "montserrat black"]
SYSTEM_FONTS = [Path("C:/Windows/Fonts/ariblk.ttf"), Path("C:/Windows/Fonts/seguibl.ttf"),
                Path("C:/Windows/Fonts/impact.ttf"), Path("C:/Windows/Fonts/arialbd.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]


def settings(cfg: dict) -> dict:
    from kdcut.config import DEFAULTS
    return {**DEFAULTS["shorts"], **(cfg.get("shorts") or {})}


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------

@dataclass
class ShortFiles:
    timelapses: list[Path] = field(default_factory=list)
    glamor: list[Path] = field(default_factory=list)
    voiceover: Path | None = None
    title: str = ""             # on-screen title; blank = the AI writes one

    def empty(self) -> bool:
        return not self.timelapses and not self.glamor


def classify(files: list[Path], title: str = "") -> ShortFiles:
    """Sort one mixed list: audio is the voiceover, glamor-ish names are glamor shots."""
    out = ShortFiles(title=title)
    for f in sorted((Path(p) for p in files), key=media._natural_key):
        ext = f.suffix.lower()
        if ext in media.AUDIO_EXTS:
            out.voiceover = out.voiceover or f
        elif ext in media.VIDEO_EXTS:
            (out.glamor if GLAMOR_NAME.search(f.stem.lower()) else out.timelapses).append(f)
    return out


def from_folder(folder: Path) -> ShortFiles | None:
    folder = Path(folder)
    if not folder.is_dir():
        return None
    files = media.list_media(folder, media.VIDEO_EXTS | media.AUDIO_EXTS)
    title = ""
    if (folder / "title.txt").is_file():
        title = (folder / "title.txt").read_text(encoding="utf-8").strip()
    short = classify(files, title)
    return None if short.empty() else short


# --------------------------------------------------------------------------------------
# The edit: how long each piece plays
# --------------------------------------------------------------------------------------

@dataclass
class Piece:
    path: Path
    kind: str           # timelapse | glamor
    start: float        # seconds into the source
    length: float       # source seconds used
    speed: float = 1.0

    @property
    def seconds(self) -> float:
        return self.length / self.speed


@dataclass
class ShortPlan:
    pieces: list[Piece]
    hold: float                 # seconds the last frame is held at the end
    total: float
    reveal_at: float | None     # when the glamor shots start (None: no glamor shots)
    voice_seconds: float        # voiceover kept (after trimming to fit)
    notes: list[str] = field(default_factory=list)


def _share(budget: float, caps: list[float]) -> list[float]:
    """Split `budget` seconds evenly over clips, none getting more than its cap."""
    out = [0.0] * len(caps)
    left, open_ = budget, [i for i, c in enumerate(caps) if c > 0]
    while left > 1e-6 and open_:
        each = left / len(open_)
        nxt = []
        for i in open_:
            give = min(each, caps[i] - out[i])
            out[i] += give
            left -= give
            if caps[i] - out[i] > 1e-6:
                nxt.append(i)
        if len(nxt) == len(open_) and each <= 1e-6:
            break
        open_ = nxt
    return out


def plan(timelapses: list[tuple[Path, float]], glamor: list[tuple[Path, float]], voice_seconds: float,
         scfg: dict) -> ShortPlan:
    """Lay out the Short. The voiceover sets the length (within min/max); the glamor shots get
    the end, and the timelapse is retimed to fill the rest."""
    max_s, min_s = float(scfg["max_seconds"]), float(scfg["min_seconds"])
    each = float(scfg["glamor_seconds"])
    notes: list[str] = []
    timelapses = [(p, d) for p, d in timelapses if d > 0.2]
    glamor = [(p, d) for p, d in glamor if d > 0.2]
    if not timelapses and not glamor:
        raise ValueError("a Short needs a timelapse or glamor shots")

    voice = max(voice_seconds, 0.0)
    if voice and LEAD + voice + TAIL > max_s:
        keep = max(max_s - LEAD - TAIL, 0.0)
        notes.append(f"The voiceover is {voice:.0f}s, longer than a Short allows here, so it's cut at "
                     f"{keep:.0f}s. Record a shorter one or raise shorts.max_seconds.")
        voice = keep
    tl_natural = sum(d for _, d in timelapses)
    gl_caps = [min(d, each) for _, d in glamor]
    if voice:
        target = LEAD + voice + TAIL
    else:
        target = (min(tl_natural, float(scfg["timelapse_seconds"])) if timelapses else 0.0) + sum(gl_caps)
    target = min(max(target, min_s), max_s)

    if not timelapses:
        reveal = target
    elif not glamor:
        reveal = 0.0
    else:
        reveal = min(sum(gl_caps), target * float(scfg["reveal_share"]))
    tl_len = target - reveal
    if timelapses and tl_natural / tl_len < MIN_SPEED:
        # Too little timelapse even at half speed: give the extra time to the reveal.
        tl_len = tl_natural / MIN_SPEED
        reveal = target - tl_len

    pieces: list[Piece] = []
    if timelapses:
        speed = round(tl_natural / tl_len, 4)
        if speed < 1:
            notes.append(f"Slowed the timelapse to {speed:.2f}x to fit the voiceover")
        pieces += [Piece(p, "timelapse", 0.0, d, speed) for p, d in timelapses]
    shown = 0.0
    if glamor:
        caps = [d for _, d in glamor]
        # Up to `each` seconds per shot first; more of each shot only if the reveal needs it.
        alloc = _share(min(reveal, sum(gl_caps)), gl_caps)
        extra = reveal - sum(alloc)
        if extra > 1e-3:
            more = _share(extra, [c - a for c, a in zip(caps, alloc)])
            alloc = [a + m for a, m in zip(alloc, more)]
        for (p, d), secs in zip(glamor, alloc):
            if secs < 0.4:
                continue
            start = max((d - secs) / 2, 0.0)      # the middle of the camera move
            pieces.append(Piece(p, "glamor", round(start, 3), round(secs, 3)))
            shown += secs
    played = sum(pc.seconds for pc in pieces)
    hold = round(max(target - played, 0.0), 3)
    if hold > 0.3:
        notes.append(f"Held the last frame {hold:.1f}s; add another glamor shot or a longer timelapse "
                     "to fill it")
    first_glam = next((i for i, pc in enumerate(pieces) if pc.kind == "glamor"), None)
    reveal_at = sum(pc.seconds for pc in pieces[:first_glam]) if first_glam is not None else None
    return ShortPlan(pieces, hold, round(played + hold, 3), reveal_at, voice, notes)


# --------------------------------------------------------------------------------------
# Voiceover and captions
# --------------------------------------------------------------------------------------

@dataclass
class CWord:
    start: float
    end: float
    text: str


def place_words(words: list, ranges: list[tuple[float, float]], lead: float = LEAD,
                limit: float | None = None) -> list[CWord]:
    """Voiceover words on the Short's clock: the kept ranges play back to back from `lead`."""
    out, at = [], lead
    for a, b in ranges:
        for w in words:
            mid = (w.start + w.end) / 2
            if a <= mid < b:
                s, e = at + max(w.start, a) - a, at + min(w.end, b) - a
                if limit is None or s < limit:
                    out.append(CWord(round(s, 3), round(min(e, limit) if limit else e, 3), w.text))
        at += b - a
    return out


def trim_ranges(ranges: list[tuple[float, float]], seconds: float) -> list[tuple[float, float]]:
    out, left = [], seconds
    for a, b in ranges:
        if left <= 0.05:
            break
        out.append((a, min(b, a + left)))
        left -= min(b - a, left)
    return out


def chunk(words: list[CWord], max_words: int = 3, max_chars: int = 16, pause: float = 0.35
          ) -> list[list[CWord]]:
    """Short punchy caption groups: break on pauses, sentence ends and length."""
    out: list[list[CWord]] = []
    cur: list[CWord] = []
    for w in words:
        if not _clean(w.text):
            continue
        if cur:
            chars = len(" ".join(_clean(x.text) for x in cur)) + 1 + len(_clean(w.text))
            if (len(cur) >= max_words or chars > max_chars or w.start - cur[-1].end > pause
                    or cur[-1].text.rstrip().endswith((".", "?", "!", ","))):
                out.append(cur)
                cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _clean(text: str) -> str:
    t = text.strip().upper().replace("{", "(").replace("}", ")").replace("\\", "/")
    return re.sub(r"[.,;:]+$", "", t)


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFC440"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def wrap_title(text: str, width: int = 14) -> str:
    words, lines, cur = _clean(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines[:3])


def build_ass(groups: list[list[CWord]], title: str, font: str, total: float, scfg: dict) -> str:
    """Subtitles for the Short: one event per spoken word, the whole group on screen with the
    current word in the highlight colour. Groups pop in; the title pops in at the start."""
    hi = ass_color(str(scfg["highlight"]))
    cy = int(H * float(scfg["caption_y"]))
    ty = int(H * float(scfg["title_y"]))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{font},100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,2,0,1,9,6,5,60,60,0,1",
        f"Style: Title,{font},104,{hi},{hi},&H00000000,&H64000000,-1,0,0,0,100,100,2,0,1,10,7,5,60,60,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title:
        lines.append(f"Dialogue: 1,{_ts(0.0)},{_ts(total)},Title,,0,0,0,,"
                     f"{{\\pos({W // 2},{ty})\\fscx60\\fscy60\\t(0,180,\\fscx100\\fscy100)}}{wrap_title(title)}")
    for gi, g in enumerate(groups):
        g_end = g[-1].end + 0.25
        if gi + 1 < len(groups):
            g_end = min(max(g_end, g[-1].end), groups[gi + 1][0].start)
        g_end = min(max(g_end, g[0].start + 0.4), total)
        for wi, w in enumerate(g):
            start = g[0].start if wi == 0 else w.start
            end = g[wi + 1].start if wi + 1 < len(g) else g_end
            if end - start < 0.02:
                continue
            parts = []
            for k, x in enumerate(g):
                word = _clean(x.text)
                if k == wi:
                    parts.append(f"{{\\c{hi}\\fscx112\\fscy112}}{word}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(word)
            pop = "\\fscx70\\fscy70\\t(0,90,\\fscx100\\fscy100)" if wi == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Caption,,0,0,0,,"
                         f"{{\\pos({W // 2},{cy}){pop}}}" + " ".join(parts))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Font, music and the reveal whoosh
# --------------------------------------------------------------------------------------

def pick_font(scfg: dict, index=None) -> Path | None:
    """Your chosen font, else the bubbliest font in your packs, else a heavy system font."""
    if scfg.get("font") and Path(scfg["font"]).expanduser().is_file():
        return Path(scfg["font"]).expanduser()
    packs = [Path(a.path) for a in (index.assets if index else []) if a.category == "font"]
    for want in BUBBLY:
        for p in packs:
            if want in re.sub(r"[_\-]+", " ", p.stem.lower()) and p.is_file():
                return p
    return next((p for p in SYSTEM_FONTS if p.is_file()), None)


def font_family(path: Path | None) -> str:
    if path is None:
        return "Arial Black"
    try:
        from PIL import ImageFont
        name = ImageFont.truetype(str(path), 20).getname()[0] or path.stem
        return name.replace(",", " ")
    except Exception:
        return path.stem


def pick_track(library: list, total: float, seed: str, history: dict | None = None):
    """An upbeat track long enough for the Short, not used in the last week if possible."""
    from kdcut import music
    offered, _ = music.for_vibe(library, "upbeat")
    if not offered:
        return None
    week = time.time() - 7 * 86400
    hist = history or {}

    def rank(t):
        e = music.energy_level(t) or 2
        return (hist.get(t.path, 0) >= week, t.duration < total * 0.8, -e)

    best = sorted(offered, key=rank)
    top = [t for t in best if rank(t) == rank(best[0])][:8]
    return random.Random(seed).choice(top)


# --------------------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------------------

def _fps(scfg: dict, first: Path) -> Fraction:
    want = media.parse_fps(scfg.get("fps"))
    if want:
        return want
    try:
        f = media.normalize_fps(media.probe(first).fps)
    except media.MediaError:
        f = Fraction(60)
    return min(f, Fraction(60))


def video_filter(p: ShortPlan, scfg: dict, fps: Fraction, ass_name: str | None, fonts_dir: str | None
                 ) -> tuple[str, str]:
    """(filter_complex for the picture, output label). Inputs 0..n-1 are the pieces."""
    f = []
    first_glam = next((i for i, pc in enumerate(p.pieces) if pc.kind == "glamor"), None)
    for i, pc in enumerate(p.pieces):
        chain = [f"setpts=(PTS-STARTPTS)/{pc.speed}", f"fps={fps}",
                 "scale=1920:1080:force_original_aspect_ratio=decrease",
                 "pad=1920:1080:(ow-iw)/2:(oh-ih)/2", "setsar=1",
                 f"trim=duration={pc.seconds:.3f}", "setpts=PTS-STARTPTS"]
        if i == first_glam and i > 0:
            chain.append(f"fade=t=in:st=0:d={FLASH}:color=white")
        f.append(f"[{i}:v]" + ",".join(chain) + f"[v{i}]")
    n = len(p.pieces)
    f.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")
    last = "cat"
    if p.hold > 0:
        f.append(f"[cat]tpad=stop_mode=clone:stop_duration={p.hold:.3f}[held]")
        last = "held"
    if scfg.get("layout") == "crop":
        f.append(f"[{last}]scale=-2:{H},crop={W}:{H}[lay]")
    else:
        fg_h = int(round(W * 9 / 16 * max(float(scfg.get("zoom", 1.0)), 1.0) / 2)) * 2
        f.append(f"[{last}]split[bgsrc][fgsrc]")
        # Blur small and scale up: looks the same as a big blur and costs a fraction.
        f.append(f"[bgsrc]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
                 f"boxblur=8:2,scale={W}:{H},eq=brightness=-0.10:saturation=1.1[bg]")
        f.append(f"[fgsrc]scale=-2:{fg_h},crop='min(iw,{W})':ih[fg]")
        f.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[lay]")
    tail = "format=yuv420p"
    if ass_name:
        tail = f"ass={ass_name}" + (f":fontsdir={fonts_dir}" if fonts_dir else "") + "," + tail
    f.append(f"[lay]{tail}[vout]")
    return ";".join(f), "vout"


def render(p: ShortPlan, dest: Path, work: Path, scfg: dict, *, voice_file: Path | None = None,
           voice_ranges: list[tuple[float, float]] | None = None, music_bed: Path | None = None,
           sfx: Path | None = None, sfx_db: float = 0.0, ass_text: str | None = None,
           font: Path | None = None, log=None) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fps = _fps(scfg, p.pieces[0].path)
    ass_name = fonts_dir = None
    if ass_text:
        (work / "short.ass").write_text(ass_text, encoding="utf-8")
        ass_name = "short.ass"
        if font is not None:
            fdir = work / "fonts"
            if fdir.exists():
                shutil.rmtree(fdir, ignore_errors=True)
            fdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(font, fdir / font.name)
            fonts_dir = "fonts"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for pc in p.pieces:
        cmd += ["-ss", f"{pc.start:.3f}", "-t", f"{pc.length:.3f}", "-i", str(Path(pc.path).resolve())]
    vf, vlabel = video_filter(p, scfg, fps, ass_name, fonts_dir)
    filters = [vf]
    n = len(p.pieces)
    audio: list[str] = []
    fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    lead_ms = int(LEAD * 1000)
    if voice_file is not None and voice_ranges:
        cmd += ["-i", str(Path(voice_file).resolve())]
        k = len(voice_ranges)
        filters.append(f"[{n}:a]{fmt},asplit={k}" + "".join(f"[vs{j}]" for j in range(k)) if k > 1
                       else f"[{n}:a]{fmt}[vs0]")
        for j, (a, b) in enumerate(voice_ranges):
            filters.append(f"[vs{j}]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,"
                           f"afade=t=in:d=0.02,afade=t=out:st={max(b - a - 0.03, 0):.3f}:d=0.03[vp{j}]")
        filters.append("".join(f"[vp{j}]" for j in range(k)) + f"concat=n={k}:v=0:a=1,"
                       f"adelay={lead_ms}|{lead_ms}[voice]")
        audio.append("[voice]")
        n += 1
    if music_bed is not None:
        cmd += ["-i", str(Path(music_bed).resolve())]
        filters.append(f"[{n}:a]{fmt}[bed]")
        audio.append("[bed]")
        n += 1
    if sfx is not None and p.reveal_at is not None:
        cmd += ["-i", str(Path(sfx).resolve())]
        at = int(max(p.reveal_at - 0.25, 0) * 1000)
        filters.append(f"[{n}:a]{fmt},volume={sfx_db:.1f}dB,adelay={at}|{at}[sfx]")
        audio.append("[sfx]")
        n += 1
    if audio:
        mix = (f"{''.join(audio)}amix=inputs={len(audio)}:normalize=0:dropout_transition=0,"
               if len(audio) > 1 else f"{audio[0]}anull,")
    else:
        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{p.total:.3f}[silence]")
        mix = "[silence]anull,"
    filters.append(f"{mix}alimiter=limit=0.95:level=disabled,apad=whole_dur={p.total:.3f},"
                   f"atrim=0:{p.total:.3f},afade=t=out:st={max(p.total - 0.6, 0):.3f}:d=0.6[aout]")
    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{vlabel}]", "-map", "[aout]",
            "-t", f"{p.total:.3f}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest.resolve())]
    proc = media.run_watched(cmd, p.total, label="Rendering the Short", log=log, stall=120,
                             timeout=1800, cwd=work)
    if proc.returncode != 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise media.MediaError((proc.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:300])
    media._PROBE_CACHE.pop(dest, None)
    return dest


# --------------------------------------------------------------------------------------
# Titles and description
# --------------------------------------------------------------------------------------

KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "on_screen_title": {"type": "string",
                            "description": "2-5 words shown big at the top the whole Short, e.g. "
                                           "'STARTER FARM BUILD'. No emoji, no hashtags."},
        "titles": {"type": "array", "items": {"type": "string"},
                   "description": "3 YouTube titles for the Short, each under 60 characters."},
        "description": {"type": "string",
                        "description": "1-2 short sentences, then a line pointing to the full episode, "
                                       "then 3-5 hashtags including #minecraft and #shorts."},
    },
    "required": ["on_screen_title", "titles", "description"],
}


def kit_without_ai(short: ShortFiles, video: dict) -> dict:
    base = short.title or _pretty((short.timelapses or short.glamor)[0].stem)
    series = video.get("series") or ""
    return {"on_screen_title": short.title or base,
            "titles": [f"{base} | Minecraft" + (f" {series}" if series else ""), f"Building a {base} in Minecraft"],
            "description": f"{base}, from the latest {series or 'episode'}. Full episode on the channel!\n\n"
                           "#minecraft #shorts #minecraftbuilding"}


def _pretty(stem: str) -> str:
    s = re.sub(r"(?i)\b(timelapse|time lapse|glamor|glamour|reveal|final|v\d+)\b", " ", stem)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\b\d{4}[ \-]\d{2}[ \-]\d{2}.*$", "", s)        # OBS/Flashback date stamps
    return " ".join(s.split()).title() or "New Build"


def write_kit(cfg: dict, video: dict, short: ShortFiles, transcript: str, use_ai: bool, caller,
              work: Path, log=print) -> dict:
    """Titles, description and the on-screen title. Cached so re-runs don't ask again."""
    sig = hashlib.sha1(json.dumps([transcript, short.title, video.get("title"), video.get("series"),
                                   [p.name for p in short.timelapses + short.glamor]]).encode()).hexdigest()[:12]
    cache = work / "short_kit.json"
    try:
        old = json.loads(cache.read_text(encoding="utf-8"))
        if old.get("sig") == sig:
            return old["kit"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    kit = kit_without_ai(short, video)
    if use_ai:
        user = (f"Channel: {cfg['channel'].get('name', '')} (Minecraft survival, building kingdoms)\n"
                f"Full episode: {video.get('title', '')}\n"
                + (f"Series: {video['series']}\n" if video.get("series") else "")
                + (f"What happens in the episode: {video['topic']}\n" if video.get("topic") else "")
                + f"Short clips: {', '.join(p.name for p in short.timelapses + short.glamor)}\n"
                + f"Voiceover: \"{transcript[:1500]}\"\n\n"
                "This is a vertical YouTube Short: a build timelapse, then a cinematic reveal of the "
                "finished build. Write the title shown on screen, three YouTube titles and the "
                "description. Titles should make someone scrolling stop: name the build, hint at the "
                "payoff. Keep it jovial, not clickbait, and never say 'cozy'.")
        if short.title:
            user += f"\nThe creator chose this on-screen title, so use it exactly: {short.title}"
        try:
            res = caller(cfg, "You write packaging for a Minecraft YouTube channel's Shorts.", user,
                         "short_kit", "Give the Short's on-screen title, titles and description.", KIT_SCHEMA)
            kit = {"on_screen_title": short.title or str(res.get("on_screen_title") or kit["on_screen_title"]),
                   "titles": [str(t) for t in res.get("titles") or []][:3] or kit["titles"],
                   "description": str(res.get("description") or kit["description"])}
        except Exception as exc:
            log(f"Couldn't write the Short's titles with the AI ({exc}); using simple ones")
    try:
        work.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"sig": sig, "kit": kit}), encoding="utf-8")
    except OSError:
        pass
    return kit


def kit_markdown(kit: dict, p: ShortPlan, video_name: str) -> str:
    out = [f"# Short: {video_name}", "", f"Length: {p.total:.0f}s", "", "## Titles",
           *[f"- {t}" for t in kit["titles"]], "", "## Description", kit["description"], "",
           "## Posting", "- In YouTube Studio set the Short's **Related video** to the full episode, "
           "so viewers can tap through to it.",
           "- Post it a few days after the episode (e.g. Tuesday or Wednesday after a Saturday upload).", ""]
    if p.notes:
        out += ["## Check", *[f"- {n}" for n in p.notes], ""]
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# All together
# --------------------------------------------------------------------------------------

def make(out: Path, cfg: dict, video: dict, short: ShortFiles, use_ai: bool = True, caller=None,
         log=print, index=None, force: bool = False) -> dict:
    """Make the Short for an edited video. Returns {'video', 'seconds', 'kit', 'notes'}."""
    from kdcut import audioprep, ducking, music, timelapse, transcribe, vad
    scfg = settings(cfg)
    out = Path(out)
    work = out / ".work" / "short"
    work.mkdir(parents=True, exist_ok=True)
    media.require_ffmpeg()
    tls = [(p, media.probe(p).duration) for p in short.timelapses if Path(p).exists()]
    gls = [(p, media.probe(p).duration) for p in short.glamor if Path(p).exists()]

    voice_file, ranges, words = None, [], []
    if short.voiceover is not None and Path(short.voiceover).exists():
        vo = Path(short.voiceover)
        words = transcribe.transcribe_file(vo, cfg, out / ".work", force=force, log=log)
        info = media.probe(vo)
        voice = vad.analyse(vo, 0, out / ".work", log=log)
        ranges = timelapse.voice_ranges(words, info.duration, cfg["edit"], voice)
        if not ranges and info.duration > 0.5:      # nothing transcribed: use it as recorded
            ranges = [(0.0, info.duration)]
        stems = audioprep.split_tracks(vo, info, 0, out / ".work" / "audio", log=log)
        voice_file = stems[0].path if stems else vo
    voice_secs = sum(b - a for a, b in ranges)
    p = plan(tls, gls, voice_secs, scfg)
    ranges = trim_ranges(ranges, p.voice_seconds) if ranges else []
    placed = place_words(words, ranges, LEAD, limit=p.total)

    transcript = " ".join(w.text.strip() for w in words)
    kit = write_kit(cfg, video, short, transcript, use_ai, caller or _default_caller(), work, log=log)
    title = kit.get("on_screen_title", "") if scfg.get("title", True) else ""

    font = pick_font(scfg, index)
    ass = build_ass(chunk(placed), title, font_family(font), p.total, scfg)

    bed, track = None, None
    mcfg = cfg.get("music", {})
    if scfg.get("music", True) and mcfg.get("enabled", True) and mcfg.get("folders"):
        try:
            lib = music.scan_library(mcfg["folders"], log=log)
            track = pick_track(lib.music, p.total, video.get("title", "") + str(short.timelapses),
                               music.load_history())
        except Exception as exc:
            log(f"Short music skipped ({exc})")
        if track is not None:
            vol = float(scfg["music_db"])
            speech = [(w.start, w.end) for w in placed]
            gains = ducking.envelope(speech, p.total, talk_db=vol, gap_db=vol + 8.0)
            try:
                bed = ducking.render_bed([music.Placement(track, 0.0, 0.0, min(track.duration, p.total))],
                                         p.total, gains, work / "music_bed.m4a",
                                         levels=[audioprep.music_gain(Path(track.path))])
            except Exception as exc:
                log(f"Short music skipped ({exc})")
                bed = None

    sfx, sfx_db = None, 0.0
    if scfg.get("reveal_sfx", True) and index is not None and p.reveal_at is not None:
        for q in ("whoosh", "swoosh", "reveal"):
            a = index.best(q, "sfx")
            if a is not None and Path(a.path).is_file():
                sfx = Path(a.path)
                try:
                    sfx_db = audioprep.sfx_gain(sfx) - 4.0
                except Exception:
                    sfx_db = -6.0
                break

    name = video.get("title") or out.name
    dest = out / "short" / f"{_safe(name)} - Short.mp4"
    log(f"Making the Short ({p.total:.0f}s)")
    render(p, dest, work, scfg, voice_file=voice_file, voice_ranges=ranges, music_bed=bed, sfx=sfx,
           sfx_db=sfx_db, ass_text=ass, font=font, log=log)
    if track is not None:
        music.record_use([track.path])
    kit_path = out / "short" / "short_kit.md"
    kit_path.write_text(kit_markdown(kit, p, name), encoding="utf-8")
    (out / "short" / "short_kit.json").write_text(json.dumps({**kit, "seconds": p.total}, indent=2,
                                                             ensure_ascii=False), encoding="utf-8")
    log(f"Short:      {dest}")
    return {"video": dest, "seconds": p.total, "kit": kit_path, "notes": p.notes,
            "music": track.name if track else "", "font": font.name if font else ""}


def _default_caller():
    from kdcut import llm
    return llm.call_tool


def _safe(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()
    return keep or "video"
