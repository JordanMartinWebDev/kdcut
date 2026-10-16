"""Where your voice really starts and stops, from the mic track itself.

Whisper's word timings are good but loose: a word's end is often marked a few tenths of a
second early (cutting off "everyth-"), and a word said before a long pause can be placed in
the wrong spot. Cut points are therefore snapped to the actual sound on the mic track.
"""

from __future__ import annotations

import json
from array import array
from dataclasses import dataclass
from pathlib import Path

from kdcut import media

STEP = 0.02          # seconds per level reading
RATE = 8000


@dataclass
class Voice:
    levels: list[float]          # dB per STEP
    threshold: float

    def _i(self, t: float) -> int:
        return min(max(int(t / STEP), 0), max(len(self.levels) - 1, 0))

    def on(self, t: float) -> bool:
        return bool(self.levels) and self.levels[self._i(t)] > self.threshold

    def voiced(self, a: float, b: float) -> float:
        """Seconds of voice between a and b."""
        if not self.levels or b <= a:
            return 0.0
        return sum(1 for i in range(self._i(a), self._i(b) + 1) if self.levels[i] > self.threshold) * STEP

    def offset_after(self, t: float, limit: float = 0.8, quiet: float = 0.12) -> float:
        """If the voice is still going at t (Whisper ended the word early), where it really stops."""
        if not self.levels or not any(self.on(t + k * STEP) for k in range(-2, 4)):
            return t
        i, end, silent = self._i(t), self._i(t + limit), 0
        last_on = i
        while i <= end and i < len(self.levels):
            if self.levels[i] > self.threshold:
                last_on, silent = i, 0
            else:
                silent += 1
                if silent * STEP >= quiet:
                    break
            i += 1
        return max(t, min((last_on + 1) * STEP, t + limit))

    def speech_end(self, t: float, back: float = 1.2) -> float:
        """Where the talking really stops near a Whisper word end t. Whisper often stretches
        the last word of a phrase well into the silence after it (about 0.6 s on real
        footage), which leaves a dead tail after each cut. If the voice is still going at t,
        this is offset_after(t); if it is already quiet, step back to the last voiced moment
        (at most `back` seconds)."""
        if not self.levels:
            return t
        if any(self.on(t + k * STEP) for k in range(-2, 4)):
            return self.offset_after(t)
        i, stop = self._i(t), self._i(t - back)
        while i > stop:
            if self.levels[i] > self.threshold:
                return min(t, (i + 1) * STEP)
            i -= 1
        return t            # nothing voiced nearby (a quiet word): trust Whisper

    def cut_between(self, a_end: float, b_start: float, before: float = 0.15,
                    after: float = 0.1) -> tuple[float, bool]:
        """The best place to cut between one stretch of talk ending (Whisper says a_end) and
        the next starting (b_start): the middle of the longest quiet moment around the
        boundary. Returns (time, clean); clean is False when the voice never stops there
        (one breath, e.g. "...that new world feeling let's go"), and the time is then the
        quietest instant."""
        lo = min(a_end, b_start) - before
        hi = b_start + after
        i0, i1 = self._i(max(lo, 0.0)), self._i(hi)
        if not self.levels or i1 <= i0:
            return b_start, True
        best, run_start = (0, i0), None
        for i in range(i0, i1 + 1):
            quiet = self.levels[i] <= self.threshold
            if quiet and run_start is None:
                run_start = i
            if run_start is not None and (not quiet or i == i1):
                end = i if not quiet else i + 1
                if end - run_start > best[0]:
                    best = (end - run_start, run_start)
                run_start = None
        length, start = best
        if length * STEP >= 0.06:
            return (start + length / 2) * STEP, True
        k = min(range(i0, i1 + 1), key=lambda i: self.levels[i])
        return k * STEP, False

    def onset_before(self, t: float, limit: float = 0.8, quiet: float = 0.1) -> float:
        """If the voice had already started before t, where it really starts."""
        if not self.levels or not any(self.on(t + k * STEP) for k in range(-3, 3)):
            return t
        i, stop, silent = self._i(t), self._i(t - limit), 0
        first_on = i
        while i >= stop and i >= 0:
            if self.levels[i] > self.threshold:
                first_on, silent = i, 0
            else:
                silent += 1
                if silent * STEP >= quiet:
                    break
            i -= 1
        return min(t, max(first_on * STEP, t - limit))


def _levels_from_pcm(raw: bytes) -> list[float]:
    import math
    n = int(RATE * STEP)
    try:
        import numpy as np
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        k = len(a) // n
        if k == 0:
            return []
        rms = np.sqrt(np.mean(a[:k * n].reshape(k, n) ** 2, axis=1))
        return [round(float(v), 1) for v in 20 * np.log10(np.maximum(rms, 1e-6))]
    except ImportError:
        a = array("h")
        a.frombytes(raw[:len(raw) - len(raw) % 2])
        out = []
        for s in range(0, len(a) - n + 1, n):
            chunk = a[s:s + n]
            rms = math.sqrt(sum(x * x for x in chunk) / n) / 32768.0
            out.append(round(20 * math.log10(max(rms, 1e-6)), 1))
        return out


def threshold_for(levels: list[float]) -> float:
    """A bit above the noise floor, but never so low that breathing counts."""
    if not levels:
        return -40.0
    vals = sorted(levels)
    floor = vals[len(vals) // 10]
    loud = vals[int(len(vals) * 0.9)]
    return max(min(floor + 12.0, loud - 15.0), -55.0)


def analyse(rec: Path, stream: int, cache_dir: Path, log=None) -> Voice | None:
    rec = Path(rec)
    st = rec.stat()
    cache = cache_dir / f"{rec.stem}.voice{stream + 1}.json"
    key = [st.st_size, int(st.st_mtime), stream, STEP, 1]
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("key") == key:
            return Voice(data["levels"], data["threshold"])
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    if log:
        log(f"Finding exactly where you talk in {rec.name}")
    try:
        proc = media.run_quiet(["ffmpeg", "-v", "error", "-i", str(rec), "-map", f"0:a:{stream}", "-vn",
                                "-af", f"aresample={RATE}:async=1:first_pts=0", "-ac", "1",
                                "-f", "s16le", "-acodec", "pcm_s16le", "-"], timeout=1800, text=False)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    levels = _levels_from_pcm(proc.stdout)
    voice = Voice(levels, threshold_for(levels))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "threshold": voice.threshold, "levels": levels}),
                         encoding="utf-8")
    except OSError:
        pass
    return voice
