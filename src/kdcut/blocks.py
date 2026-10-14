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
    pauses: list[tuple[float, float]] = field(default_factory=list)  # long silences inside the block

    @property
    def duration(self) -> float:
        return self.end - self.start


# Words Whisper often leaves at the end of a phrase that really start the next one
# ("...across water all | right it's fine"). They're moved to the following block so a cut
# between blocks never strands half a sentence.
DANGLING = {"and", "all", "i", "so", "but", "oh", "hey", "um", "uh", "the", "a", "an", "to", "of",
            "then", "because", "like", "we", "now", "okay", "ok", "alright", "well", "or", "just",
            "you", "this", "my", "with", "for", "if", "when", "it's", "i'm", "let's", "also"}


def ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?"))


CUE_PAUSE_BEFORE = 0.15  # Whisper timings are loose, so this is lenient
CUE_PAUSE_AFTER = 0.3    # this is what separates "cut that." from "cut that tree"


def find_cues(words: list[Word]) -> list[tuple[int, int, str]]:
    """Return (first_word_index, last_word_index_exclusive, kind) for each cue."""
    normed = [_norm(w.text) for w in words]

    def isolated(a: int, b: int) -> bool:
        before = (a == 0 or words[a].start - words[a - 1].end >= CUE_PAUSE_BEFORE
                  or words[a - 1].text.endswith((".", ",", "!", "?")))
        after = (b >= len(words) or words[b].start - words[b - 1].end >= CUE_PAUSE_AFTER
                 or words[b - 1].text.endswith((".", "!", "?"))
                 # "cut that i... also" -> the stray word starts the next sentence
                 or (b + 1 < len(words) and _norm(words[b].text) in DANGLING
                     and words[b + 1].start - words[b].end >= 0.6))
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

        block_words: list[list[Word]] = []

        def flush(cue: str | None = None, upto: int | None = None):
            part = current[:upto] if upto is not None else list(current)
            if part:
                src_blocks.append(Block(
                    id="", source=src_idx, start=part[0].start, end=part[-1].end,
                    text=" ".join(w.text for w in part), cue=cue,
                ))
                block_words.append(part)
                del current[:len(part)]

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
                if pause >= gap or (sentence_end and pause >= gap / 2):
                    flush()
                elif too_long:
                    flush(upto=_best_split(current + [w], max_seconds))
            current.append(w)
            i += 1
        flush()
        _move_dangling(src_blocks, block_words)

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


def _best_split(ws: list[Word], max_seconds: float) -> int:
    """Where to break an over-long run of speech: at a sentence end if there is one past the
    first 40%, otherwise at the longest pause. Returns how many words go in the first part."""
    t0 = ws[0].start
    best, best_score = len(ws) - 1, -1.0
    for k in range(1, len(ws)):
        if ws[k].start - t0 < max_seconds * 0.4:
            continue
        score = max(ws[k].start - ws[k - 1].end, 0.0) + (10.0 if ends_sentence(ws[k - 1].text) else 0.0)
        if ws[k - 1].text.endswith(","):
            score += 2.0
        if _norm(ws[k - 1].text) in DANGLING:
            score -= 5.0
        if score >= best_score:
            best, best_score = k, score
    return best


def _move_dangling(src_blocks: list[Block], block_words: list[list[Word]], within: float = 8.0) -> None:
    k = 0
    while k + 1 < len(src_blocks):
        b, n = src_blocks[k], src_blocks[k + 1]
        ws, nws = block_words[k], block_words[k + 1]
        moved = 0
        while (ws and moved < 2 and not b.cue and not n.cue
               and _norm(ws[-1].text) in DANGLING
               and not ends_sentence(ws[-1].text) and not ws[-1].text.endswith(",")
               and n.start - ws[-1].end < within):
            nws.insert(0, ws.pop())
            moved += 1
        if moved:
            gap = (nws[moved - 1].end, nws[moved].start)
            if gap[1] - gap[0] > 0.5:
                n.pauses.insert(0, gap)       # "all ... right": the wait in between can be cut
            n.start, n.text = nws[0].start, " ".join(w.text for w in nws)
            if not ws:
                del src_blocks[k], block_words[k]
                continue
            b.end, b.text = ws[-1].end, " ".join(w.text for w in ws)
        k += 1


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
