"""Captions (.srt) and re-timing chapters against a final export."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kdcut.transcribe import Word


@dataclass
class TWord:
    start: float
    end: float
    text: str


def words_on_timeline(per_source: list[list[Word]], recordings: list[Path], timeline,
                      extra: dict[str, list[Word]] | None = None) -> list[TWord]:
    """Map the transcript onto the edited timeline (only words that survived the edit).
    `extra` adds words of connected audio clips (voiceovers): file path -> words."""
    index = {str(p): i for i, p in enumerate(recordings)}
    per_source = list(per_source)
    for path, ws in (extra or {}).items():
        index[str(path)] = len(per_source)
        per_source.append(ws)
    extra_clips = [c for c in timeline.connected if str(c.path) in (extra or {})]
    out: list[TWord] = []
    for clip in [*timeline.main, *extra_clips]:
        src = index.get(str(clip.path))
        if src is None:
            continue
        a = timeline.seconds(clip.src_in)
        b = timeline.seconds(clip.src_in + clip.dur)
        base = timeline.seconds(clip.rec)
        for w in per_source[src]:
            mid = (w.start + w.end) / 2
            if a <= mid < b:
                out.append(TWord(base + max(w.start, a) - a, base + min(w.end, b) - a, w.text))
    out.sort(key=lambda w: w.start)
    return out


def _ts(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(words: list, max_chars: int = 42, max_seconds: float = 5.0, pause: float = 0.6) -> str:
    """Readable caption lines: break on pauses, sentence ends, length and duration."""
    cues: list[list] = []
    cur: list = []
    for w in words:
        if cur:
            text_len = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            if (w.start - cur[-1].end > pause or text_len > max_chars
                    or w.end - cur[0].start > max_seconds or cur[-1].text.endswith((".", "?", "!"))):
                cues.append(cur)
                cur = []
        cur.append(w)
    if cur:
        cues.append(cur)
    out = []
    for n, c in enumerate(cues, 1):
        end = max(c[-1].end, c[0].start + 0.8)
        if n < len(cues):
            end = min(end, cues[n][0].start - 0.01)
        out.append(f"{n}\n{_ts(c[0].start)} --> {_ts(end)}\n{' '.join(x.text for x in c)}\n")
    return "\n".join(out)


def _norm(text: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9' ]", " ", text.lower()).split() if t]


def find_phrase(phrase: str, words: list, after: float = 0.0) -> float | None:
    """Where does this phrase start in the word list? Tolerant of small transcription
    differences: best window match of the first few words, must be a clear match."""
    target = _norm(phrase)[:6]
    if len(target) < 2:
        return None
    toks = [(_norm(w.text) or [""])[0] for w in words]
    best, best_i = 0.0, None
    for i in range(len(toks) - len(target) + 1):
        if words[i].start < after:
            continue
        hits = sum(1 for a, b in zip(target, toks[i:i + len(target)]) if a == b)
        score = hits / len(target)
        if score > best:
            best, best_i = score, i
            if score == 1.0:
                break
    return words[best_i].start if best_i is not None and best >= 0.6 else None


def find_phrase_span(phrase: str, words: list, after: float = 0.0, before: float = float("inf")
                     ) -> tuple[float, float] | None:
    """(start, end) of a short phrase in the word list, matched tolerantly like find_phrase.
    Words are searched between `after` and `before`."""
    target = _norm(phrase)
    if not target:
        return None
    pool = [w for w in words if after <= w.start <= before]
    toks = [(_norm(w.text) or [""])[0] for w in pool]
    n = len(target)
    best, best_i = 0.0, None
    for i in range(len(toks)):
        window = toks[i:i + n]
        if not window:
            break
        score = sum(1 for a, b in zip(target, window) if a == b) / n
        if score > best:
            best, best_i = score, i
            if score == 1.0:
                break
    if best_i is None or best < (0.6 if n >= 2 else 1.0):
        return None
    last = pool[min(best_i + n, len(pool)) - 1]
    return pool[best_i].start, last.end


def retime_chapters(chapters: list[tuple[str, str, float]], words: list) -> list[tuple[float, str]]:
    """chapters: (title, anchor phrase, old time). Find each anchor in the final export's
    transcript; keep the old time when it can't be found. First chapter is always 0:00."""
    out, after = [], 0.0
    for i, (title, phrase, old) in enumerate(chapters):
        t = 0.0 if i == 0 else (find_phrase(phrase, words, after) if phrase else None)
        if t is None:
            t = old
        out.append((t, title))
        after = t
    return out
