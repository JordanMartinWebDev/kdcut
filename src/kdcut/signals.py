"""How lively each part of a recording is, measured from the footage itself (no AI cost).

The transcript says what you said. These numbers say how it felt and what the screen was
doing, so the AI editor can tell a tense, wordless cave crawl from two minutes of sorting
chests:

  energy   how animated your voice is: loudness, swings, laughs and shouts, talking pace
  action   how much the picture changes (running, fighting, exploring vs standing still)
  menus    how much of the time an inventory, chest or crafting screen is open
  hits     sudden loud game sounds (hits, explosions, mobs, damage)

Everything is scored 1-5 relative to the rest of the episode, so a calm building episode
still has its livelier moments marked. The AI reads them next to the transcript:

    B0123 [14:02 9s] {energy 4 · action 2} oh no no no, creeper!
          [quiet 45s after: action 1 · menus 80%]
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kdcut import media
from kdcut.blocks import Block

VERSION = 1
W, H = 160, 90                     # frame size for the picture checks
GUI_GRAY = 198                     # Minecraft's inventory/chest/crafting panel colour (#C6C6C6)


@dataclass
class RecordingSignals:
    frames: list[float] = field(default_factory=list)   # keyframe times
    motion: list[float] = field(default_factory=list)   # change since the previous keyframe, per second
    menu: list[float] = field(default_factory=list)     # share of the middle of the frame that's GUI panel
    game: list[tuple[float, float]] = field(default_factory=list)   # (time, dB) every 0.5 s


# --------------------------------------------------------------------------------------
# Measuring (cached per recording)
# --------------------------------------------------------------------------------------

def menu_share(rgb: bytes, w: int = W, h: int = H) -> float:
    """How much a small frame looks like an open inventory/chest/crafting screen: share of
    the middle that is flat GUI-panel gray (#C6C6C6), counted only when the world around it
    is dimmed, as Minecraft does behind menus. Stone, snow and clouds are textured or bright
    around the edges, so they don't count."""
    try:
        import numpy as np
        a = np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3).astype(np.int16)
        mid = a[h // 5:h * 4 // 5, w // 4:w * 3 // 4]
        flat = (np.abs(mid - GUI_GRAY) <= 4).all(axis=2).mean()
        edges = np.concatenate([a[:, :w // 5], a[:, w * 4 // 5:]], axis=1)
        outer = (edges[..., 0] * 3 + edges[..., 1] * 6 + edges[..., 2]).mean() / 10
    except ImportError:
        hit = tot = 0
        for y in range(h // 5, h * 4 // 5):
            for x in range(w // 4, w * 3 // 4):
                i = (y * w + x) * 3
                tot += 1
                hit += all(abs(rgb[i + c] - GUI_GRAY) <= 4 for c in range(3))
        flat = hit / tot if tot else 0.0
        lum = [(rgb[i] * 3 + rgb[i + 1] * 6 + rgb[i + 2]) / 10
               for y in range(h) for x in [*range(w // 5), *range(w * 4 // 5, w)] for i in [(y * w + x) * 3]]
        outer = sum(lum) / len(lum)
    return float(flat) if outer < MENU_DIM else 0.0


MENU_DIM = 60          # mean brightness at the edges below which the world counts as dimmed
MENU_FLAT = 0.02       # flat panel share that means a menu is open


def _luma(rgb: bytes):
    try:
        import numpy as np
        a = np.frombuffer(rgb, dtype=np.uint8).reshape(-1, 3).astype(np.int16)
        return (a[:, 0] * 3 + a[:, 1] * 6 + a[:, 2]) // 10
    except ImportError:
        return [(rgb[i] * 3 + rgb[i + 1] * 6 + rgb[i + 2]) // 10 for i in range(0, len(rgb), 3)]


def _diff(a, b) -> float:
    try:
        import numpy as np
        return float(np.abs(a - b).mean())
    except ImportError:
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def picture(path: Path, timeout: float = 3600) -> tuple[list[float], list[float], list[float]]:
    """(times, motion, menu share) from the keyframes only, so a 2-hour file takes a minute."""
    def run(sync: list[str]):
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-skip_frame", "nokey",
               "-i", str(path), "-an", "-vf", f"scale={W}:{H},showinfo", *sync,
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return media.run_quiet(cmd, timeout=timeout, text=False)

    try:
        proc = run(["-fps_mode", "passthrough"])          # one output frame per keyframe
        if proc.returncode != 0 and b"fps_mode" in (proc.stderr or b""):
            proc = run(["-vsync", "passthrough"])         # older ffmpeg
    except (OSError, subprocess.TimeoutExpired):
        return [], [], []
    err = proc.stderr.decode("utf-8", "replace")
    times = []
    for line in err.splitlines():
        if "Parsed_showinfo" in line and "pts_time:" in line:
            try:
                times.append(float(line.split("pts_time:", 1)[1].split()[0]))
            except ValueError:
                pass
    n = W * H * 3
    raw = proc.stdout or b""
    count = min(len(raw) // n, len(times))
    motion, menu, prev, prev_t = [], [], None, None
    for k in range(count):
        frame = raw[k * n:(k + 1) * n]
        luma = _luma(frame)
        if prev is None:
            motion.append(0.0)
        else:
            diff = _diff(luma, prev)
            motion.append(round(diff / max(times[k] - prev_t, 0.5) * 2.0, 2))   # per ~2 s
        prev, prev_t = luma, times[k]
        menu.append(round(menu_share(frame), 3))
    if motion:
        motion[0] = motion[1] if len(motion) > 1 else 0.0
    return times[:count], motion, menu


def measure(path: Path, info: media.MediaInfo, mic_setting, cache_dir: Path, log=print) -> RecordingSignals:
    from kdcut import vision
    path = Path(path)
    st = path.stat()
    key = [st.st_size, int(st.st_mtime), VERSION]
    cache = cache_dir / f"{path.stem}.signals.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("key") == key:
            d = data["signals"]
            return RecordingSignals(d["frames"], d["motion"], d["menu"], [tuple(x) for x in d["game"]])
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    log(f"Measuring the action in {path.name} (picture, menus, game sounds)")
    sig = RecordingSignals()
    if info.has_video:
        sig.frames, sig.motion, sig.menu = picture(path)
    track = vision.game_audio_track(info, mic_setting)
    if track is not None:
        sig.game = [(round(t, 2), round(v, 1)) for t, v in vision.loudness(path, track)]
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "signals": asdict(sig)}), encoding="utf-8")
    except OSError:
        pass
    return sig


# --------------------------------------------------------------------------------------
# Scoring blocks and quiet stretches
# --------------------------------------------------------------------------------------

@dataclass
class Span:
    energy: float | None = None     # raw values; scored 1-5 against the episode afterwards
    action: float | None = None
    menus: float | None = None      # 0-1
    hits: int = 0


def _window(times: list[float], values: list[float], a: float, b: float) -> list[float]:
    out = [v for t, v in zip(times, values) if a <= t <= b]
    if not out and times:          # short span between keyframes: nearest keyframe
        import bisect
        i = min(bisect.bisect_left(times, (a + b) / 2), len(times) - 1)
        out = [values[i]]
    return out


def voice_energy(voice, a: float, b: float, words: int) -> float | None:
    """Loud, varied, fast talking scores high; flat, slow narration low."""
    if voice is None or not voice.levels or b <= a:
        return None
    from kdcut.vad import STEP
    lv = voice.levels[int(a / STEP):int(b / STEP) + 1]
    on = [x for x in lv if x > voice.threshold]
    if len(on) < 5:
        return None
    mean = sum(on) / len(on)
    spread = math.sqrt(sum((x - mean) ** 2 for x in on) / len(on))
    peak = sorted(on)[int(len(on) * 0.95)]
    pace = words / max(b - a, 0.5)
    return mean + 0.6 * spread + 0.4 * (peak - mean) + 3.0 * pace


def span(sig: RecordingSignals | None, voice, a: float, b: float, words: int = 0,
         spike_db: float | None = None) -> Span:
    out = Span()
    if voice is not None and words:
        out.energy = voice_energy(voice, a, b, words)
    if sig is None:
        return out
    if sig.frames:
        m = _window(sig.frames, sig.motion, a, b)
        out.action = sum(m) / len(m) if m else None
        g = _window(sig.frames, sig.menu, a, b)
        out.menus = sum(1 for x in g if x >= MENU_FLAT) / len(g) if g else None
    if sig.game and spike_db is not None:
        out.hits = sum(1 for t, v in sig.game if a <= t <= b and v >= spike_db)
    return out


def _scorer(values: list[float]):
    """1-5 by where a value falls in this episode (its percentile)."""
    import bisect
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 2:
        return lambda v: None

    def score(v):
        if v is None:
            return None
        lo, hi = bisect.bisect_left(vals, v), bisect.bisect_right(vals, v)
        rank = ((lo + hi - 1) / 2) / (len(vals) - 1)
        return 1 + min(int(rank * 5), 4)
    return score


def game_spike_level(signals: dict[int, RecordingSignals]) -> dict[int, float]:
    out = {}
    for src, sig in signals.items():
        vals = sorted(v for _, v in sig.game if v > -100)
        if vals:
            out[src] = vals[len(vals) // 2] + 14.0
    return out


@dataclass
class Notes:
    block_tags: dict[str, str] = field(default_factory=dict)     # id -> "{energy 4 · action 2}"
    quiet: dict[str, str] = field(default_factory=dict)          # id -> line about the stretch after it
    quiet_seconds: dict[str, float] = field(default_factory=dict)
    scores: dict[str, dict] = field(default_factory=dict)        # id -> {"energy": 4, ...} (for tests/teaser)


def describe(blocks: list[Block], signals: dict[int, RecordingSignals], voices: dict,
             durations: list[float], min_quiet: float = 8.0) -> Notes:
    spikes = game_spike_level(signals)
    spans: dict[str, Span] = {}
    quiet: dict[str, tuple[Span, float]] = {}
    by_src: dict[int, list[Block]] = {}
    for b in blocks:
        by_src.setdefault(b.source, []).append(b)
    for src, bl in by_src.items():
        sig, voice = signals.get(src), voices.get(src)
        for k, b in enumerate(bl):
            spans[b.id] = span(sig, voice, b.start, b.end, len(b.text.split()), spikes.get(src))
            end = bl[k + 1].start if k + 1 < len(bl) else (durations[src] if src < len(durations) else b.end)
            if end - b.end >= min_quiet:
                quiet[b.id] = (span(sig, None, b.end, end, 0, spikes.get(src)), end - b.end)
    all_spans = list(spans.values()) + [s for s, _ in quiet.values()]
    energy = _scorer([s.energy for s in spans.values()])
    action = _scorer([s.action for s in all_spans])
    notes = Notes()
    for bid, s in spans.items():
        e, a = energy(s.energy), action(s.action)
        parts = []
        if e is not None:
            parts.append(f"energy {e}")
        if a is not None:
            parts.append(f"action {a}")
        if s.menus is not None and s.menus >= 0.3:
            parts.append(f"menus {round(s.menus * 100)}%")
        if s.hits:
            parts.append(f"hits {s.hits}")
        if parts:
            notes.block_tags[bid] = "{" + " · ".join(parts) + "}"
        notes.scores[bid] = {"energy": e, "action": a, "menus": s.menus, "hits": s.hits}
    for bid, (s, secs) in quiet.items():
        a = action(s.action)
        parts = [f"action {a}"] if a is not None else []
        if s.menus is not None:
            parts.append(f"menus {round(s.menus * 100)}%")
        if s.hits:
            parts.append(f"hits {s.hits}")
        notes.quiet[bid] = f"[quiet {secs:.0f}s after: " + (" · ".join(parts) or "no data") + "]"
        notes.quiet_seconds[bid] = secs
    return notes


def as_notes(n: Notes) -> dict[str, list[str]]:
    """Quiet-stretch lines in the shape the planner prints under each block."""
    return {bid: [line] for bid, line in n.quiet.items()}
