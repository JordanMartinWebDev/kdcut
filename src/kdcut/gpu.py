"""NVIDIA GPU support for the speech model.

The installer stays small by not shipping NVIDIA's CUDA libraries (~1 GB). The app's
Settings window offers a one-time download instead: it fetches NVIDIA's official
cuBLAS and cuDNN packages from PyPI and keeps only the DLLs, in ~/.kdcut/cuda.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from kdcut.config import HOME_DIR

CUDA_DIR = HOME_DIR / "cuda"

# Versions known to work with faster-whisper / CTranslate2 4.x (CUDA 12 + cuDNN 9).
PACKAGES = [
    ("nvidia-cublas-cu12", "12.4.5.8", "12."),
    ("nvidia-cudnn-cu12", "9.1.0.70", "9."),
]
REQUIRED_DLLS = ["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"]


def has_nvidia_gpu() -> bool:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "-L"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "GPU" in out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def libs_installed() -> bool:
    if sys.platform != "win32":
        return True  # Linux/macOS developers install CUDA their own way
    return all((CUDA_DIR / d).exists() for d in REQUIRED_DLLS)


def status() -> str:
    gpu = has_nvidia_gpu()
    if not gpu:
        return "No NVIDIA GPU detected: transcription will use the CPU (slower, still works)."
    if libs_installed():
        return "NVIDIA GPU ready."
    return "NVIDIA GPU found, but GPU support isn't downloaded yet."


def _wheel_url(package: str, version: str, major_prefix: str) -> tuple[str, int]:
    def pick(files):
        for f in files:
            if f["filename"].endswith(".whl") and "win_amd64" in f["filename"]:
                return f["url"], int(f.get("size") or 0)
        return None

    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/{version}/json", timeout=60) as r:
            got = pick(json.load(r)["urls"])
            if got:
                return got
    except Exception:
        pass
    # Pinned version unavailable: take the newest release with the same major version.
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=60) as r:
        data = json.load(r)
    versions = sorted((v for v in data["releases"] if v.startswith(major_prefix)),
                      key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")], reverse=True)
    for v in versions:
        got = pick(data["releases"][v])
        if got:
            return got
    raise RuntimeError(f"No Windows download found for {package}")


def install(progress=None, log=print, cancelled=lambda: False) -> None:
    """Download cuBLAS + cuDNN and extract their DLLs into CUDA_DIR."""
    CUDA_DIR.mkdir(parents=True, exist_ok=True)
    plan = []
    for pkg, ver, major in PACKAGES:
        url, size = _wheel_url(pkg, ver, major)
        plan.append((pkg, url, size))
    total = sum(s for _, _, s in plan) or 1
    done = 0
    with tempfile.TemporaryDirectory() as tmp:
        for pkg, url, size in plan:
            log(f"Downloading {pkg} ({size / 1e6:.0f} MB)")
            dest = Path(tmp) / url.rsplit("/", 1)[-1]
            with urllib.request.urlopen(url, timeout=120) as r, dest.open("wb") as fh:
                while True:
                    if cancelled():
                        raise RuntimeError("Cancelled")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(min(done / total, 1.0))
            log(f"Unpacking {pkg}")
            with zipfile.ZipFile(dest) as z:
                for member in z.namelist():
                    if member.lower().endswith(".dll") and "/bin/" in member:
                        target = CUDA_DIR / Path(member).name
                        with z.open(member) as src, target.open("wb") as out:
                            shutil.copyfileobj(src, out)
            dest.unlink()
    missing = [d for d in REQUIRED_DLLS if not (CUDA_DIR / d).exists()]
    if missing:
        raise RuntimeError(f"GPU support download finished but these files are missing: {missing}")
    log("GPU support installed.")
