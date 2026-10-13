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
    """Make the NVIDIA CUDA DLLs findable on Windows.

    They come either from pip's nvidia-cublas/cudnn wheels (developer install) or from the
    desktop app's one-time "GPU support" download (see kdcut.gpu).
    """
    if sys.platform != "win32":
        return
    from kdcut import gpu
    dirs = [d for base in map(Path, sys.path) for d in (base / "nvidia").glob("*/bin")]
    dirs.append(gpu.CUDA_DIR)
    for bin_dir in dirs:
        if not bin_dir.is_dir():
            continue
        try:
            os.add_dll_directory(str(bin_dir))
        except OSError:
            pass
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


_MODEL = None


_MODEL_KEY = None


def _load_model(cfg: dict, log=print):
    global _MODEL, _MODEL_KEY
    key = (cfg["whisper"].get("model"), cfg["whisper"].get("device"), cfg["whisper"].get("compute_type"))
    if _MODEL is not None and _MODEL_KEY == key:
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
    log(f"Loading speech model '{wc.get('model')}' (the first time, this downloads ~1-3 GB)")
    for dev, ct in attempts:
        try:
            _MODEL = WhisperModel(wc.get("model", "large-v3"), device=dev, compute_type=ct)
            _MODEL_KEY = key
            log(f"Speech model running on {'your NVIDIA GPU' if dev == 'cuda' else 'the CPU (slower)'}")
            return _MODEL
        except Exception as exc:  # CUDA libs missing, out of memory, ...
            last_err = exc
            log(f"Could not start on {dev}: {exc}")
    raise RuntimeError(f"Could not load Whisper: {last_err}")


def _load_wav(path: Path):
    """16 kHz mono PCM WAV -> float32 samples.

    We decode with ffmpeg ourselves, so faster-whisper's PyAV-based loader (which breaks
    whenever PyAV changes its API) is never used.
    """
    import wave

    import numpy as np
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise RuntimeError(f"Unexpected audio format in {path.name}")
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_file(path: Path, cfg: dict, cache_dir: Path, force: bool = False,
                    on_progress=None, log=print) -> list[Word]:
    info = media.probe(path)
    track = media.pick_mic_track(info, cfg["whisper"].get("mic_track", "auto"))
    key = _cache_key(path, track, cfg["whisper"].get("model", ""))
    cache_file = cache_dir / f"{path.stem}.words.json"
    if not force:
        cached = load_cached(cache_file, key)
        if cached is not None:
            log(f"Already transcribed: {path.name}")
            if on_progress:
                on_progress(1.0)
            return cached

    wav = media.extract_audio(path, track, cache_dir / f"{path.stem}.16k.wav")
    model = _load_model(cfg, log)
    log(f"Transcribing {path.name} (audio track {track + 1})")
    segments, _ = model.transcribe(
        _load_wav(wav),
        language=cfg["whisper"].get("language") or None,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,  # keeps long gameplay sessions from looping
    )
    words: list[Word] = []
    for seg in segments:
        if on_progress and info.duration:
            on_progress(min(seg.end / info.duration, 1.0))
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(start=round(w.start, 3), end=round(w.end, 3), text=text))
    save_cache(cache_file, key, words)
    try:
        wav.unlink()
    except OSError:
        pass
    if on_progress:
        on_progress(1.0)
    log(f"{path.name}: {len(words)} words")
    return words
