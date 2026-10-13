"""Group words into numbered speech blocks and apply spoken editing cues.

Spoken cues let you edit while recording:
  "cut that" / "scratch that" / "let me redo that"  -> drops what you just said
  "clip that" / "clip it"                           -> marks what you just did as a highlight
  "new chapter" / "chapter break"                   -> starts a YouTube chapter here
A cue only counts when you say it on its own, with a short pause before and after,
so "I'm gonna cut that tree down" is left alone. The cue words themselves are removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kdcut.transcribe import Word

CUES: dict[str, list[tuple[str, ...]]] = {
    "cut": [("cut", "that"), ("cut", "cut"), ("scratch", "that"), ("redo", "that"),
            ("let", "me", "redo", "that"), ("let", "me", "redo")],
    "highlight": [("clip", "that"), ("clip", "it")],
    "chapter": [("new", "chapter"), ("chapter", "break")],
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9']", "", text.lower())


@dataclass
class Block:
    id: str
    source: int            # index into the recordings list
    start: float           # seconds in the source file
    end: float
    text: str
    cue: str | None = None  # set when the block is a spoken cue
    flags: set[str] = field(default_factory=set)  # forced_drop, highlight, chapter

    @property
    def duration(self) -> float:
        return self.end - self.start


CUE_PAUSE_BEFORE = 0.15  # Whisper timings are loose, so this is lenient
CUE_PAUSE_AFTER = 0.3    # this is what separates "cut that." from "cut that tree"


def find_cues(words: list[Word]) -> list[tuple[int, int, str]]:
    """Return (first_word_index, last_word_index_exclusive, kind) for each cue."""
    normed = [_norm(w.text) for w in words]

    def isolated(a: int, b: int) -> bool:
        before = (a == 0 or words[a].start - words[a - 1].end >= CUE_PAUSE_BEFORE
                  or words[a - 1].text.endswith((".", ",", "!", "?")))
        after = (b >= len(words) or words[b].start - words[b - 1].end >= CUE_PAUSE_AFTER
                 or words[b - 1].text.endswith((".", "!", "?")))
        return before and after

    hits = []
    i = 0
    while i < len(words):
        matched = None
        for kind, phrases in CUES.items():
            for phrase in sorted(phrases, key=len, reverse=True):
                n = len(phrase)
                if tuple(normed[i:i + n]) == phrase and isolated(i, i + n):
                    matched = (i, i + n, kind)
                    break
            if matched:
                break
        if matched:
            hits.append(matched)
            i = matched[1]
        else:
            i += 1
    return hits


def build_blocks(
    per_source_words: list[list[Word]],
    gap: float = 0.8,
    max_seconds: float = 20.0,
) -> list[Block]:
    blocks: list[Block] = []

    for src_idx, words in enumerate(per_source_words):
        cue_at = {start: (end, kind) for start, end, kind in find_cues(words)}
        current: list[Word] = []
        src_blocks: list[Block] = []

        def flush(cue: str | None = None):
            if current:
                src_blocks.append(Block(
                    id="", source=src_idx, start=current[0].start, end=current[-1].end,
                    text=" ".join(w.text for w in current), cue=cue,
                ))
                current.clear()

        i = 0
        while i < len(words):
            if i in cue_at:
                end, kind = cue_at[i]
                flush()
                current.extend(words[i:end])
                flush(cue=kind)
                i = end
                continue
            w = words[i]
            if current:
                pause = w.start - current[-1].end
                too_long = w.end - current[0].start > max_seconds
                sentence_end = current[-1].text.endswith((".", "?", "!"))
                if pause >= gap or too_long or (sentence_end and pause >= gap / 2):
                    flush()
            current.append(w)
            i += 1
        flush()

        # Apply cue effects to neighbours within this source.
        for k, b in enumerate(src_blocks):
            if not b.cue:
                continue
            b.flags.add("forced_drop")
            prev = src_blocks[k - 1] if k > 0 else None
            nxt = src_blocks[k + 1] if k + 1 < len(src_blocks) else None
            if b.cue == "cut" and prev and not prev.cue:
                prev.flags.add("forced_drop")
            elif b.cue == "highlight" and prev and not prev.cue:
                prev.flags.add("highlight")
            elif b.cue == "chapter" and nxt:
                nxt.flags.add("chapter")
        blocks.extend(src_blocks)

    for n, b in enumerate(blocks, start=1):
        b.id = f"B{n:04d}"
    return blocks


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
