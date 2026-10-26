"""Turn blocks + an edit plan into a frame-accurate multi-track timeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from kdcut.blocks import Block
from kdcut.planner import Plan


@dataclass
class Range:
    source: int
    start: float
    end: float
    block_ids: list[str] = field(default_factory=list)


@dataclass
class Clip:
    path: Path
    src_in: int        # frames into the source media
    dur: int           # frames
    rec: int           # frames from timeline start
    lane: int = 0      # 0 = main story, >0 video tracks above, <0 audio tracks below
    enable: str = "all"  # all | video | audio
    role: str = "main"   # main | broll | overlay | sfx | timelapse | voiceover | ...
    name: str = ""
    is_image: bool = False
    volume_db: float | None = None   # clip gain (background music)

    @property
    def rec_end(self) -> int:
        return self.rec + self.dur


@dataclass
class Marker:
    rec: int
    name: str
    kind: str = "chapter"  # chapter | note


@dataclass
class Timeline:
    name: str
    fps: Fraction
    width: int
    height: int
    main: list[Clip] = field(default_factory=list)
    connected: list[Clip] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)

    @property
    def duration(self) -> int:
        return self.main[-1].rec_end if self.main else 0

    def frames(self, seconds: float) -> int:
        return int(round(seconds * self.fps))

    def seconds(self, frames: int) -> float:
        return float(Fraction(frames) / self.fps)


def teaser_range(block: Block, pick: dict, words: list, duration: float, voice=None) -> Range:
    """The cold open, cut to the exact words the AI chose: from just before `start_words` to the
    end of `end_words`, kept between 3 and 12 seconds. Falls back to the block's speech."""
    from kdcut.captions import find_phrase_span
    lead = min(max(float(pick.get("lead_in", 0.6) or 0.0), 0.0), 3.0)
    # Speech in the block, skipping a stray word left before a pause ("oh ... dude dude").
    talk_start = block.pauses[-1][1] if block.pauses else block.start
    span = find_phrase_span(pick.get("start_words") or "", words, block.start - 1.0, block.end + 1.0)
    first = span[0] if span else talk_start
    end_span = find_phrase_span(pick.get("end_words") or "", words, first, first + 15.0)
    last = end_span[1] if end_span else None
    if last is None or last <= first:
        last = first + float(pick.get("seconds", 6) or 6) - lead
    if voice is not None:
        first = voice.onset_before(first)
        last = voice.speech_end(last)
    start = max(first - lead, 0.0)
    end = last + 0.4
    end = min(max(end, start + 3.0), start + 12.0, duration)
    return Range(block.source, start, end, [block.id])


def kept_ranges(blocks: list[Block], plan: Plan, durations: list[float], edit: dict,
                voices: dict | None = None, break_after: set[str] | None = None) -> list[Range]:
    """Source ranges to keep. `voices` (source -> vad.Voice) snaps cut points to where the
    voice really starts and stops, since Whisper's word timings are a little loose.
    A range always ends after a block in `break_after` (something gets inserted there)."""
    break_after = break_after or set()
    pad_b = float(edit.get("pad_before", 0.15))
    pad_a = float(edit.get("pad_after", 0.35))
    max_pause = float(edit.get("keep_pauses_up_to", 2.5))

    ranges: list[Range] = []
    by_source: dict[int, list[Block]] = {}
    for b in blocks:
        by_source.setdefault(b.source, []).append(b)

    def is_dropped(b: Block) -> bool:
        return "forced_drop" in b.flags or b.id in plan.drop

    for src in sorted(by_source):
        src_blocks = by_source[src]
        limit = durations[src] if src < len(durations) else float("inf")
        v = (voices or {}).get(src)
        onset = (lambda t: v.onset_before(t)) if v else (lambda t: t)
        offset = (lambda t: v.offset_after(t)) if v else (lambda t: t)
        speech_end = (lambda t: v.speech_end(t)) if v else (lambda t: t)
        cur: Range | None = None
        cur_spoken_end = 0.0       # where the last kept words really end
        for i, b in enumerate(src_blocks):
            if is_dropped(b):
                if cur:
                    prev = src_blocks[i - 1] if i > 0 else None
                    if v and prev is not None and not is_dropped(prev) and b.start - prev.end < 1.0:
                        cur.end = min(cur.end, max(v.cut_between(prev.end, b.start)[0], cur.start + 0.1))
                    else:
                        cur.end = min(cur.end, max(onset(b.start), cur_spoken_end))
                    ranges.append(cur)
                    cur = None
                continue
            prev = src_blocks[i - 1] if i > 0 else None
            nxt = src_blocks[i + 1] if i + 1 < len(src_blocks) else None
            # Real end of the cut words before this block, but never past where this block starts.
            prev_end = 0.0 if prev is None else (
                min(offset(prev.end), max(prev.end, b.start - 0.05)) if is_dropped(prev) else prev.end)
            if v and prev is not None and is_dropped(prev) and b.start - prev.end < 1.0:
                # Cut talk right before this block: start in the quiet moment between them,
                # not where Whisper's word timings happen to put the boundary.
                prev_end = min(v.cut_between(prev.end, b.start)[0], b.start)
            # A block can hold a long wait between words ("all ... right"); treat each side
            # as its own piece so the wait is trimmed like any other pause.
            pieces, at = [], b.start
            for a, z in b.pauses:
                if at < a < z < b.end:
                    pieces.append((at, a))
                    at = z
            pieces.append((at, b.end))
            if v:
                pieces = _split_on_silence(pieces, v, onset, speech_end, max_pause)
            for k, (p_start, p_end) in enumerate(pieces):
                last = k == len(pieces) - 1
                if v and p_end - p_start < 1.5 and v.voiced(p_start - 0.1, p_end + 0.1) < 0.1:
                    continue        # Whisper put a word where nothing was said: keep no frames
                following = pieces[k + 1][0] if not last else (nxt.start if nxt else limit)
                real_start = max(onset(p_start), prev_end if k == 0 else pieces[k - 1][1])
                real_end = min(speech_end(p_end), max(p_end, following - 0.05))
                s = max(real_start - pad_b, prev_end if k == 0 else pieces[k - 1][1], 0.0)
                ext = plan.extend.get(b.id, 0.0) if last else 0.0
                if ext and nxt is not None and not is_dropped(nxt) and nxt.start - p_end < ext + 1.0:
                    # He talks again before the extend would end: that's just a pause, not
                    # something to show. Treat it like any other pause (cut it).
                    ext = 0.0
                e = real_end + pad_a + ext
                if not last:
                    e = min(e, max(pieces[k + 1][0], real_end))
                elif nxt is not None and is_dropped(nxt):
                    if v and nxt.start - p_end < 1.0:
                        # The next words are cut: stop in the quiet moment before them. (The
                        # voice can run straight on, so where it "stops" may be inside them.)
                        cut = max(v.cut_between(p_end, nxt.start)[0], p_start + 0.1)
                        e = min(e, cut)
                        real_end = min(real_end, cut)
                    else:
                        e = min(e, max(onset(nxt.start), real_end))
                e = min(e, limit)
                ends_here = cur is not None and cur.block_ids[-1] in break_after and cur.block_ids[-1] != b.id
                # Keep the pause only when the real silence (voice to voice, not the padded
                # edges) is short; padding never turns a long pause into a kept one.
                short_pause = s <= cur.end or real_start - cur_spoken_end <= max_pause if cur else False
                if cur and short_pause and not ends_here:
                    cur.end = max(cur.end, e)
                    if b.id not in cur.block_ids:
                        cur.block_ids.append(b.id)
                else:
                    if cur:
                        ranges.append(cur)
                    cur = Range(src, s, e, [b.id])
                cur_spoken_end = real_end
        if cur:
            ranges.append(cur)
    return [r for r in ranges if r.end - r.start > 0.05]


def _split_on_silence(pieces: list[tuple[float, float]], v, onset, speech_end, max_pause: float
                      ) -> list[tuple[float, float]]:
    """Split each piece where the mic is silent for longer than `max_pause` (a thinking pause
    inside a sentence), and start each piece where the voice really starts: Whisper often
    puts the first word a few tenths of a second early."""
    out: list[tuple[float, float]] = []
    for a, z in pieces:
        lo = v.onset_after(onset(a))
        hi = max(speech_end(z), lo)
        runs = v.runs(lo, hi, max_pause) if hi - lo > 0.05 else []
        if not runs:
            out.append((a, z))          # a quiet word the mic barely caught: trust Whisper
            continue
        if len(runs) == 1:
            out.append((runs[0][0], max(runs[0][1], min(z, hi))))
            continue
        out += [(x, y) for x, y in runs[:-1]] + [(runs[-1][0], max(runs[-1][1], min(z, hi)))]
    return out


class _Lanes:
    """Avoid stacking two connected clips on the same track at the same time."""

    def __init__(self):
        self.busy: dict[int, list[tuple[int, int]]] = {}

    def place(self, preferred: int, rec: int, dur: int) -> int:
        step = 1 if preferred > 0 else -1
        lane = preferred
        for _ in range(4):
            spans = self.busy.setdefault(lane, [])
            if all(rec >= e or rec + dur <= s for s, e in spans):
                spans.append((rec, rec + dur))
                return lane
            lane += step
        self.busy.setdefault(lane, []).append((rec, rec + dur))
        return lane


def build(name: str, recordings: list[Path], blocks: list[Block], plan: Plan,
          fps: Fraction, width: int, height: int, durations: list[float], edit: dict,
          prefix: Range | None = None, voices: dict | None = None,
          inserts: dict[str, list[Clip]] | None = None) -> tuple[Timeline, dict[str, int], dict[str, int]]:
    """Lay out the main story. Returns the timeline plus block-id -> timeline frame maps
    for the start and end of every kept block (used to place B-roll, SFX, chapters).

    `prefix` is an optional cold open: a short teaser copied from later in the video and
    placed before everything else.

    `inserts` puts standalone clips (timelapses) into the story: block id -> clips that play
    right after that block, as scenes of their own. A block that didn't survive the edit
    moves its clips to the kept block before it; the key "__end__" means after the story."""
    tl = Timeline(name=name, fps=fps, width=width, height=height)
    by_id = {b.id: b for b in blocks}
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    rec = 0
    if prefix is not None:
        src_in = tl.frames(prefix.start)
        dur = tl.frames(prefix.end) - src_in
        if dur > 0:
            tl.main.append(Clip(path=recordings[prefix.source], src_in=src_in, dur=dur, rec=0,
                                role="cold_open", name=f"Cold open - {recordings[prefix.source].stem}"))
            rec = dur
    inserts = {k: list(v) for k, v in (inserts or {}).items() if v}
    ranges = kept_ranges(blocks, plan, durations, edit, voices)
    if inserts:
        kept = {bid for r in ranges for bid in r.block_ids}
        order = [b.id for b in blocks]
        moved: dict[str, list[Clip]] = {}
        for key, clips in inserts.items():
            anchor = key
            if key != "__end__" and key not in kept:
                i = order.index(key) if key in order else len(order)
                anchor = next((order[j] for j in range(min(i, len(order) - 1), -1, -1) if order[j] in kept),
                              "__end__")
            moved.setdefault(anchor, []).extend(clips)
        inserts = moved
        ranges = kept_ranges(blocks, plan, durations, edit, voices, break_after=set(inserts) - {"__end__"})

    def place_inserts(key: str, rec: int) -> int:
        for c in inserts.pop(key, []):
            c.rec = rec
            tl.main.append(c)
            rec += c.dur
        return rec

    last_range = {bid: i for i, r in enumerate(ranges) for bid in r.block_ids}
    for i, r in enumerate(ranges):
        src_in = tl.frames(r.start)
        dur = tl.frames(r.end) - src_in
        if dur > 0:
            tl.main.append(Clip(path=recordings[r.source], src_in=src_in, dur=dur, rec=rec,
                                name=recordings[r.source].stem))
            for bid in r.block_ids:
                b = by_id[bid]
                starts.setdefault(bid, rec + max(tl.frames(b.start) - src_in, 0))
                ends[bid] = min(rec + tl.frames(b.end) - src_in, rec + dur)
            rec += dur
        if last_range.get(r.block_ids[-1]) == i:     # not halfway through a block
            rec = place_inserts(r.block_ids[-1], rec)
    for key in list(inserts):          # anything left (its block had no frames): after the story
        rec = place_inserts(key, rec)
    return tl, starts, ends


def nearest_kept(bid: str, blocks: list[Block], starts: dict[str, int]) -> str | None:
    """If the AI anchored something on a block that got cut, use the next kept block."""
    if bid in starts:
        return bid
    ids = [b.id for b in blocks]
    if bid not in ids:
        return None
    for later in ids[ids.index(bid):]:
        if later in starts:
            return later
    return None
