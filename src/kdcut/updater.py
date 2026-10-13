"""In-app updates from the GitHub "latest" release.

The Windows build publishes KDcut-Setup.exe on a release named "KDcut X.Y.Z". The app
compares that with its own version, downloads the installer and runs it silently; the
installer closes KDcut, upgrades it in place (settings in ~/.kdcut are untouched) and
starts it again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from kdcut import __version__

REPO = "JordanMartinWebDev/kdcut"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "KDcut-Setup.exe"


def current_version() -> str:
    """The installer build stamps kdcut/_build.py with the exact build number."""
    try:
        from kdcut._build import VERSION  # type: ignore
        return VERSION
    except ImportError:
        return __version__


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+(?:\.\d+)+)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(remote: str, local: str) -> bool:
    r, l = parse_version(remote), parse_version(local)
    if not r or not l:
        return False
    width = max(len(r), len(l))
    return r + (0,) * (width - len(r)) > l + (0,) * (width - len(l))


@dataclass
class Update:
    version: str
    url: str
    size: int
    notes: str


def check(timeout: float = 10.0) -> Update | None:
    """Return the available update, or None if up to date (or offline)."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "KDcut-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return from_release(data, current_version())


def from_release(data: dict, local: str) -> Update | None:
    version = ".".join(map(str, parse_version(data.get("name") or data.get("tag_name") or "")))
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET), None)
    if not version or asset is None or not is_newer(version, local):
        return None
    return Update(version=version, url=asset["browser_download_url"], size=int(asset.get("size") or 0),
                  notes=(data.get("body") or "").strip())


def download(update: Update, progress=None, cancelled=lambda: False) -> Path:
    dest = Path(tempfile.gettempdir()) / f"KDcut-Setup-{update.version}.exe"
    req = urllib.request.Request(update.url, headers={"User-Agent": "KDcut-updater"})
    done = 0
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or update.size or 0)
        while True:
            if cancelled():
                raise RuntimeError("Cancelled")
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(min(done / total, 1.0))
    if update.size and dest.stat().st_size != update.size:
        raise RuntimeError("The download was incomplete. Please try again.")
    return dest


def run_installer(installer: Path) -> None:
    """Start the installer detached; the caller should quit right after."""
    if sys.platform != "win32":
        raise RuntimeError("Updates can only be installed on Windows.")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=flags, close_fds=True, cwd=os.path.dirname(installer))
