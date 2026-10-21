"""Spoken clues the creator leaves for the edit, inside ordinary talk.

Unlike the spoken commands in blocks.py ("cut that", "clip that"), these are part of what
he says to the viewer, so they stay in the video. They mark how the story should be cut:

  build cue  "let's build this thing", "cue the music", "let's queue a time lapse",
             "let's get digging", "montage", "time to build": the work starts right after.
             The quiet stretch after it is the build (a montage, a timelapse or a
             fast-forward), and a new, upbeat song starts there.
  time skip  "it's nighttime", "okay, it's been two minutes", "a few hours later": the
             wait before it is cut with a hard cut, not sped up or held.
"""

from __future__ import annotations

import re

from kdcut.blocks import Block

_NUM = r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|a few|a couple(?: of)?|several|\d+)"

BUILD_CUE = [
    r"\b(?:cue|queue|q)(?:\s+up)?\s+(?:the\s+|some\s+)?(?:\w+\s+)?music\b",
    r"\b(?:cue|queue|start|starting|begin)\s+(?:a|the|up\s+a|up\s+the)?\s*(?:time\s?-?\s?lapse|montage)\b",
    r"\b(?:time\s?-?\s?lapse|montage)\s+time\b",
    r"\bcontinue\s+the\s+(?:montage|time\s?-?\s?lapse)\b",
    r"\blet'?s\s+(?:go\s+ahead\s+and\s+)?(?:build|start\s+building|get\s+building|get\s+to\s+(?:it|work|building)|get\s+started|get\s+digging|start\s+digging)\b",
    r"\btime\s+to\s+(?:build|start\s+building|get\s+to\s+work)\b",
    r"\b(?:without\s+further\s+ado)\b",
]

TIME_SKIP = [
    r"it'?s\s+(?:now\s+)?(?:night\s?time|nighttime|night|morning|daytime|day\s?time|the\s+next\s+day)\b",
    r"(?:it'?s\s+been|after)\s+" + _NUM + r"\s+(?:minutes?|hours?|days?|nights?|mins?)\b",
    _NUM + r"\s+(?:minutes?|hours?|days?|nights?)\s+later\b",
    r"(?:fast\s+forward|skip(?:ping)?\s+ahead|time\s+skip)\b",
]

_BUILD = [re.compile(p, re.I) for p in BUILD_CUE]
_SKIP = [re.compile(p, re.I) for p in TIME_SKIP]


def is_build_cue(text: str) -> bool:
    t = " ".join(text.split())
    return any(p.search(t) for p in _BUILD)


_FILLER = re.compile(r"^(?:(?:okay|ok|alright|all right|alrighty|so|and|well|oh|guys|right|now|yeah|um|uh)[,.!]?\s+)+", re.I)


def is_time_skip(text: str) -> bool:
    """Only at the start of a line ('Okay guys, it's been two minutes'): it opens the next
    scene. 'Before it's night' in the middle of a sentence is just talk."""
    head = _FILLER.sub("", " ".join(text.split()[:14]))
    return any(p.match(head) for p in _SKIP)


def mark(blocks: list[Block]) -> tuple[int, int]:
    """Flag blocks with 'build_cue' / 'time_skip'. Returns (build cues, time skips)."""
    nb = ns = 0
    for b in blocks:
        if b.cue or "forced_drop" in b.flags:
            continue
        if is_build_cue(b.text):
            b.flags.add("build_cue")
            nb += 1
        if is_time_skip(b.text):
            b.flags.add("time_skip")
            ns += 1
    return nb, ns


def skip_before(blocks: list[Block]) -> set[str]:
    """Blocks whose quiet stretch afterwards is a wait the next line skips over."""
    out = set()
    for a, b in zip(blocks, blocks[1:]):
        if a.source == b.source and "time_skip" in b.flags:
            out.add(a.id)
    return out


def enforce(plan, blocks: list[Block]) -> list[str]:
    """A wait the creator skips with 'it's nighttime' is cut, never sped up or held.
    Returns notes for the review."""
    notes = []
    waits = skip_before(blocks)
    for kind in ("fast", "montage"):
        items = getattr(plan, kind)
        keep = [x for x in items if x.get("id") not in waits]
        for x in items:
            if x.get("id") in waits:
                notes.append(f"Cut the wait after {x['id']} instead of a {kind} (you skip it: time skip)")
        setattr(plan, kind, keep)
    for bid in list(plan.extend):
        if bid in waits:
            plan.extend.pop(bid)
    return notes
