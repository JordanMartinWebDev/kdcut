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
    role: str = "main"   # main | broll | overlay | sfx
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


def cold_open_range(block: Block, seconds: float, duration: float) -> Range:
    seconds = min(max(float(seconds), 3.0), 12.0)
    start = max(block.start - 0.15, 0.0)
    return Range(block.source, start, min(start + seconds, duration), [block.id])


def kept_ranges(blocks: list[Block], plan: Plan, durations: list[float], edit: dict) -> list[Range]:
    pad_b = float(edit.get("pad_before", 0.15))
    pad_a = float(edit.get("pad_after", 0.35))
    max_pause = float(edit.get("keep_pauses_up_to", 2.5))

    ranges: list[Range] = []
    by_source: dict[int, list[Block]] = {}
    for b in blocks:
        by_source.setdefault(b.source, []).append(b)

    for src in sorted(by_source):
        src_blocks = by_source[src]
        limit = durations[src] if src < len(durations) else float("inf")
        cur: Range | None = None
        for i, b in enumerate(src_blocks):
            dropped = "forced_drop" in b.flags or b.id in plan.drop
            if dropped:
                if cur:
                    cur.end = min(cur.end, b.start)
                    ranges.append(cur)
                    cur = None
                continue
            prev_end = src_blocks[i - 1].end if i > 0 else 0.0
            nxt = src_blocks[i + 1] if i + 1 < len(src_blocks) else None
            s = max(b.start - pad_b, prev_end, 0.0)
            e = b.end + pad_a + plan.extend.get(b.id, 0.0)
            if nxt is not None and ("forced_drop" in nxt.flags or nxt.id in plan.drop):
                e = min(e, nxt.start)
            e = min(e, limit)
            if cur and s - cur.end <= max_pause:
                cur.end = max(cur.end, e)
                cur.block_ids.append(b.id)
            else:
                if cur:
                    ranges.append(cur)
                cur = Range(src, s, e, [b.id])
        if cur:
            ranges.append(cur)
    return [r for r in ranges if r.end - r.start > 0.05]


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
          prefix: Range | None = None) -> tuple[Timeline, dict[str, int], dict[str, int]]:
    """Lay out the main story. Returns the timeline plus block-id -> timeline frame maps
    for the start and end of every kept block (used to place B-roll, SFX, chapters).

    `prefix` is an optional cold open: a short teaser copied from later in the video and
    placed before everything else."""
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
    for r in kept_ranges(blocks, plan, durations, edit):
        src_in = tl.frames(r.start)
        dur = tl.frames(r.end) - src_in
        if dur <= 0:
            continue
        tl.main.append(Clip(path=recordings[r.source], src_in=src_in, dur=dur, rec=rec,
                            name=recordings[r.source].stem))
        for bid in r.block_ids:
            b = by_id[bid]
            starts[bid] = rec + max(tl.frames(b.start) - src_in, 0)
            ends[bid] = min(rec + tl.frames(b.end) - src_in, rec + dur)
        rec += dur
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
