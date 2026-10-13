"""Speech-to-text with word timestamps (faster-whisper, local GPU), cached per recording."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from kdcut import media


@dataclass
class Word:
    start: float
    end: float
    text: str


def _cache_key(path: Path, track: int, model: str) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": int(st.st_mtime), "track": track, "model": model}


def load_cached(cache_file: Path, key: dict) -> list[Word] | None:
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("key") != key:
        return None
    return [Word(**w) for w in data["words"]]


def save_cache(cache_file: Path, key: dict, words: list[Word]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({"key": key, "words": [asdict(w) for w in words]}, ensure_ascii=False),
        encoding="utf-8",
    )


def _add_windows_cuda_dlls() -> None:
    """pip-installed nvidia-cublas/cudnn wheels put DLLs where Windows won't look by default."""
    if sys.platform != "win32":
        return
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if nvidia.is_dir():
            for bin_dir in nvidia.glob("*/bin"):
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    pass
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


_MODEL = None


def _load_model(cfg: dict):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    _add_windows_cuda_dlls()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install \"kdcut[all]\" (or pip install faster-whisper)"
        ) from exc
    wc = cfg["whisper"]
    device = wc.get("device", "auto")
    compute = wc.get("compute_type", "auto")
    attempts = []
    if device in ("auto", "cuda"):
        attempts.append(("cuda", "float16" if compute == "auto" else compute))
    if device in ("auto", "cpu"):
        attempts.append(("cpu", "int8" if compute == "auto" else compute))
    last_err = None
    for dev, ct in attempts:
        try:
            _MODEL = WhisperModel(wc.get("model", "large-v3"), device=dev, compute_type=ct)
            print(f"  whisper: {wc.get('model')} on {dev} ({ct})")
            return _MODEL
        except Exception as exc:  # CUDA libs missing, out of memory, ...
            last_err = exc
            print(f"  whisper: could not start on {dev}: {exc}")
    raise RuntimeError(f"Could not load Whisper: {last_err}")


def transcribe_file(path: Path, cfg: dict, cache_dir: Path, force: bool = False) -> list[Word]:
    info = media.probe(path)
    track = media.pick_mic_track(info, cfg["whisper"].get("mic_track", "auto"))
    key = _cache_key(path, track, cfg["whisper"].get("model", ""))
    cache_file = cache_dir / f"{path.stem}.words.json"
    if not force:
        cached = load_cached(cache_file, key)
        if cached is not None:
            print(f"  transcript cached: {path.name} ({len(cached)} words)")
            return cached

    wav = media.extract_audio(path, track, cache_dir / f"{path.stem}.16k.wav")
    model = _load_model(cfg)
    print(f"  transcribing {path.name} (audio track {track + 1}) ...")
    segments, _ = model.transcribe(
        str(wav),
        language=cfg["whisper"].get("language") or None,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,  # keeps long gameplay sessions from looping
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(start=round(w.start, 3), end=round(w.end, 3), text=text))
    save_cache(cache_file, key, words)
    try:
        wav.unlink()
    except OSError:
        pass
    print(f"  {len(words)} words")
    return words
