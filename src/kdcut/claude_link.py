"""Hook KDcut's channel stats up to the Claude desktop app, and refresh them every morning.

- `connect()` adds KDcut's stats server to the Claude desktop app's config (other servers in
  there are kept; a backup is written first). Claude then calls it during chats.
- `schedule_daily()` adds a Windows scheduled task that saves a stats snapshot each morning,
  so the numbers are there even when the PC is asleep later.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "kdcut"
TASK_NAME = "KDcut channel stats"


def config_paths() -> list[Path]:
    """Where the Claude desktop app keeps its config on this computer (existing ones first)."""
    out: list[Path] = []
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        out.append(appdata / "Claude" / "claude_desktop_config.json")
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        for pkg in sorted((local / "Packages").glob("Claude_*")) if (local / "Packages").is_dir() else []:
            out.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        out.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        out.append(Path.home() / ".config" / "Claude" / "claude_desktop_config.json")
    existing = [p for p in out if p.parent.is_dir()]
    return existing or out[:1]


def server_entry() -> dict:
    """How the Claude app should start the stats server: the installed kdcut-mcp.exe, or this
    Python when running from source."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("kdcut-mcp.exe")
        return {"command": str(exe), "args": []}
    return {"command": sys.executable, "args": ["-m", "kdcut.mcp_server"]}


def add_server(path: Path, entry: dict) -> bool:
    """Put the entry under mcpServers in one config file. True if the file changed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Couldn't read {path} (it isn't valid JSON), so it was left alone.") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} doesn't look like a Claude config, so it was left alone.")
    servers = data.setdefault("mcpServers", {})
    if servers.get(SERVER_NAME) == entry:
        return False
    if path.is_file():
        shutil.copyfile(path, path.with_name(path.name + ".backup"))
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True


def connect() -> list[Path]:
    entry = server_entry()
    paths = config_paths()
    for p in paths:
        add_server(p, entry)
    return paths


def connected() -> bool:
    for p in config_paths():
        try:
            if SERVER_NAME in json.loads(p.read_text(encoding="utf-8")).get("mcpServers", {}):
                return True
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return False


# --------------------------------------------------------------------------------------
# Daily snapshot (Windows Task Scheduler)
# --------------------------------------------------------------------------------------

def snapshot_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --stats-snapshot'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py = pyw if pyw.is_file() else Path(sys.executable)
    return f'"{py}" -m kdcut.cli stats'


def schedule_daily(at: str = "07:00") -> None:
    if sys.platform != "win32":
        raise RuntimeError("The morning refresh uses Windows Task Scheduler.")
    proc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", at, "/TN", TASK_NAME,
                           "/TR", snapshot_command()], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "schtasks failed")


def unschedule_daily() -> None:
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def scheduled() -> bool:
    if sys.platform != "win32":
        return False
    proc = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode == 0
