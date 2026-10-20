"""B-roll: know what is in each clip, and put the right part of it where you talk about it.

Two steps:

1. `look()` samples a few frames from every B-roll clip and has the AI write a short
   timeline of it ("0-9s walking out of the base; 9-31s swimming, breaking kelp; ...").
   The editor sees that timeline, so it can match a clip to what you say even when the
   file name is vague, and pick just the part it needs instead of the whole file.

2. `place()` puts each pick on the timeline at the exact words you say ("got some kelp"),
   not at the start of the transcript block they're in (blocks run up to 20 seconds).
   Picks never stack on top of each other: an earlier one ends where the next begins.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from kdcut import llm, media

VERSION = 1
FRAMES = 12            # at most this many frames per clip
EVERY = 3.0            # seconds between frames for short clips
MIN_SECONDS = 3.0      # shortest B-roll worth showing
DEFAULT_SECONDS = 6.0  # when nothing says how long
LEAD = 0.25            # start this long before the first word
SEARCH = 25.0          # look for the words this far past the block's end

SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "description": "What the clip shows, in order, as 1-6 parts covering the whole clip.",
            "items": {"type": "object", "properties": {
                "from": {"type": "number", "description": "Start, seconds into the clip."},
                "to": {"type": "number", "description": "End, seconds into the clip."},
                "shows": {"type": "string", "description": "Under 12 words, concrete: what is on screen."},
            }, "required": ["from", "to", "shows"]},
        },
    },
    "required": ["segments"],
}

SYSTEM = (
    "You describe B-roll clips from a Minecraft let's play, so an editor can match them to what "
    "the creator says. You get frames from one clip, each labelled with its time. Split the clip "
    "into the parts a viewer would notice (traveling, a biome, a mob, building, mining, a "
    "structure, an item) and say plainly what each part shows. Use Minecraft names."
)


@dataclass
class Segment:
    start: float
    end: float
    shows: str


def _key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"v{VERSION}|{path}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def frame_times(duration: float) -> list[float]:
    """Where to sample a clip: every few seconds, at most FRAMES, never on the very last frame."""
    if duration <= 0:
        return [0.0]
    n = int(min(FRAMES, max(2, duration // EVERY + 1)))
    step = duration / n
    return [round(step * (i + 0.5), 2) for i in range(n)]


def look(clips: dict[str, Path], cfg: dict, work: Path, caller=llm.call_tool, log=print
         ) -> dict[str, list[Segment]]:
    """name -> what the clip shows over time. Cached per file in work/broll_looks.json."""
    cache_path = work / "broll_looks.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    out: dict[str, list[Segment]] = {}
    changed = False
    for name, path in clips.items():
        try:
            k = _key(path)
        except OSError:
            continue
        hit = cache.get(name)
        if hit and hit.get("key") == k:
            out[name] = [Segment(**s) for s in hit["segments"]]
            continue
        info = media.probe(path)
        is_image = path.suffix.lower() in media.IMAGE_EXTS or not info.has_video
        times = [0.0] if is_image else frame_times(info.duration)
        frames_dir = work / "broll_frames" / k
        content: list[dict] = []
        for t in times:
            img = frames_dir / f"{t:07.2f}.jpg"
            try:
                if not img.exists():
                    media.extract_frame(path, t, img, width=512)
                content.append({"type": "text", "text": f"{t:.0f}s"})
                content.append(llm.image_block(img, max_side=512))
            except (media.MediaError, OSError):
                continue
        if not content:
            continue
        log(f"Looking at B-roll {name}")
        length = 0.0 if is_image else info.duration
        content.append({"type": "text", "text": f"Clip '{name}', {length:.0f}s long. Describe it by calling the tool."})
        try:
            res = caller(cfg, SYSTEM, content, "describe_clip", "Describe what the clip shows over time.", SCHEMA)
        except Exception as exc:          # the edit still works with just the file names
            log(f"Couldn't look at B-roll {name}: {exc}")
            continue
        segs = clean_segments(res.get("segments", []), length)
        out[name] = segs
        cache[name] = {"key": k, "segments": [s.__dict__ for s in segs]}
        changed = True
    if changed:
        try:
            cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return out


def clean_segments(raw: list, length: float) -> list[Segment]:
    segs = []
    for s in raw if isinstance(raw, list) else []:
        try:
            a, b = float(s.get("from", 0)), float(s.get("to", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if length > 0:
            a, b = min(max(a, 0.0), length), min(max(b, 0.0), length)
        if b > a or length == 0:
            segs.append(Segment(round(a, 1), round(b, 1), str(s.get("shows", ""))[:100]))
    return sorted(segs, key=lambda s: s.start)


def describe(segs: list[Segment]) -> str:
    """One line for the editor: '0-9s walking out of the base; 9-31s breaking kelp'."""
    return "; ".join(f"{s.start:.0f}-{s.end:.0f}s {s.shows}" for s in segs)


# --------------------------------------------------------------------------------------
# Placing


def source_to_timeline(tl, path: Path, t: float) -> int | None:
    """Timeline frame showing second `t` of a recording, or None if that moment was cut."""
    f = tl.frames(t)
    for c in tl.main:
        if c.role == "main" and Path(c.path) == Path(path) and c.src_in <= f < c.src_in + c.dur:
            return c.rec + (f - c.src_in)
    return None


@dataclass
class Pick:
    name: str
    path: Path
    rec: int          # timeline frame it starts on
    src_in: int       # frames into the B-roll file
    dur: int          # frames
    reason: str = ""
    exact: bool = False   # placed at the spoken words (not just the block)


def plan_pick(item: dict, path: Path, length: float, block, words: list, recording: Path,
              tl, block_rec: int | None) -> Pick | None:
    """Where on the timeline, and which part of the file, for one B-roll choice."""
    from kdcut.captions import find_phrase_span
    rec, exact, spoken = None, False, None
    if block is not None and item.get("start_words"):
        span = find_phrase_span(item["start_words"], words, block.start - 0.5, block.end + SEARCH)
        if span:
            rec = source_to_timeline(tl, recording, max(span[0] - LEAD, 0.0))
            exact = rec is not None
            if exact and item.get("end_words"):
                end = find_phrase_span(item["end_words"], words, span[0], span[0] + 60.0)
                if end and end[1] > span[0]:
                    spoken = end[1] - span[0] + LEAD + 0.5
    if rec is None:
        rec = block_rec
    if rec is None:
        return None

    # Which part of the file.
    is_still = length <= 0
    try:
        a = float(item.get("clip_from", 0) or 0)
        b = float(item.get("clip_to", 0) or 0)
    except (TypeError, ValueError):
        a = b = 0.0
    if is_still:
        a = 0.0
        want = spoken or float(item.get("duration") or DEFAULT_SECONDS)
    else:
        a = min(max(a, 0.0), max(length - MIN_SECONDS, 0.0))
        if b > a:
            want = b - a                      # the part the editor picked
        else:
            want = spoken or float(item.get("duration") or DEFAULT_SECONDS)
        want = min(want, length - a)
    want = max(want, min(MIN_SECONDS, length if not is_still else MIN_SECONDS))
    dur = min(tl.frames(want), tl.duration - rec)
    if dur <= 0:
        return None
    return Pick(name=path.name, path=path, rec=rec, src_in=tl.frames(a), dur=dur,
                reason=str(item.get("reason", "")), exact=exact)


def no_stacking(picks: list[Pick], fps_frames) -> list[Pick]:
    """In timeline order; each one ends where the next begins. One that would be left
    shorter than MIN_SECONDS gives way to the next instead."""
    picks = sorted(picks, key=lambda p: p.rec)
    out: list[Pick] = []
    min_len = fps_frames(MIN_SECONDS)
    for p in picks:
        if out and out[-1].rec + out[-1].dur > p.rec:
            prev = out[-1]
            room = p.rec - prev.rec
            if room >= min_len:
                prev.dur = room
            elif prev.exact and not p.exact:
                continue            # keep the one that's on its words
            else:
                out.pop()
        out.append(p)
    return out
