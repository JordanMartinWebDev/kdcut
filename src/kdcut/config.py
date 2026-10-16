"""Configuration: built-in defaults <- ~/.kdcut/config.toml <- <project>/kdcut.toml."""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

HOME_DIR = Path.home() / ".kdcut"
USER_CONFIG = HOME_DIR / "config.toml"

DEFAULTS: dict[str, Any] = {
    "channel": {
        "name": "KDPlays",
        # Free text the AI reads before every edit: your style, running jokes, series names.
        "style_notes": "",
    },
    "llm": {
        "model": "claude-sonnet-5-5",
        "api_key": "",            # set from the app's Settings window
        "api_key_env": "ANTHROPIC_API_KEY",
        "max_tokens": 16000,
        # Transcript blocks sent per request. 2-hour sessions are split into windows.
        "window_blocks": 600,
    },
    "whisper": {
        "model": "large-v3",
        "device": "auto",          # auto | cuda | cpu
        "compute_type": "auto",    # auto | float16 | int8_float16 | int8
        "language": "en",
        # Which OBS audio track holds your mic. "auto" picks a track titled "mic",
        # otherwise track 1 (the mix). Use a number (1-based, like OBS) to force one.
        "mic_track": "auto",
    },
    "edit": {
        "pad_before": 0.15,        # seconds kept before each spoken block
        "pad_after": 0.3,          # seconds kept after each spoken block
        "keep_pauses_up_to": 0.6,  # thinking pauses longer than this are cut (snappy jump cuts)
        "block_gap": 0.8,          # silence that splits speech into blocks
        "max_block_seconds": 20.0,
        "cold_open": True,         # start with a short teaser of the best moment
        "speedup_badge": True,     # small fast-forward badge on sped-up stretches
        "timelapse_transition": "dip",   # dip: quick fade through black into and out of a timelapse | cut
        "overlays": False,         # AI-placed item spins / text labels (off: you add graphics yourself)
        "end_screen": True,        # add an end screen laid out for YouTube's end screen elements
        "end_screen_seconds": 20,  # YouTube allows 5-20 s
        "end_screen_motion": True, # animate it (soft blend in, boxes pop in, gentle motion) instead of a still
        "end_screen_pose": "point_side",   # pose from your characters folder
        "end_screen_labels": ["WATCH NEXT", "MORE {series}"],
    },
    "vision": {
        "enabled": False,          # let the AI look at frames from quiet stretches
        "min_gap": 8,              # seconds without talking before it takes a look
        "max_frames": 120,         # cost cap per video
    },
    "music": {
        "enabled": True,
        "folders": [],             # your licensed music (e.g. Epidemic Sound downloads)
        "volume_db": -20,          # background level; voice stays on top
        "duck": True,              # dip the music while you talk
        "vibe": "upbeat",          # upbeat: happy/adventurous, medium-high energy | chill | any
    },
    "thumbnails": {
        "enabled": True,
        "style": "showcase",       # showcase: the build/view is the hook | classic: hook words + icons
        "characters_dir": "",      # folder of transparent PNG poses of your skin
        "themes_dir": str(HOME_DIR / "themes"),
        "variants": 3,
    },
    "assets": {
        # Folders containing editing packs (sound effects, overlays, transitions).
        "packs": [],
        "index_path": str(HOME_DIR / "asset_index.json"),
        "cache_dir": str(HOME_DIR / "cache"),
    },
    "app": {
        "projects_dir": str(Path.home() / "Videos" / "KDcut"),
        "check_updates": True,     # look for a newer KDcut when the app starts
    },
    "timeline": {
        "fps": "auto",             # auto = match first recording, or e.g. 60 / 30 / 29.97
        "broll_lane": 1,
        "overlay_lane": 2,
        "sfx_lane": -1,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


SETTINGS_FILE = HOME_DIR / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    """Settings saved by the desktop app (same shape as the config sections)."""
    path = path or SETTINGS_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict, path: Path | None = None) -> None:
    path = path or SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(project_dir: Path | None = None, user_config: Path | None = None,
         settings_file: Path | None = None) -> dict:
    """defaults <- ~/.kdcut/config.toml <- app settings.json <- <project>/kdcut.toml"""
    cfg = _merge(DEFAULTS, _read(user_config or USER_CONFIG))
    cfg = _merge(cfg, load_settings(settings_file))
    if project_dir is not None:
        cfg = _merge(cfg, _read(Path(project_dir) / "kdcut.toml"))
    return cfg


PROJECT_TEMPLATE = """\
# Per-video settings. Anything here overrides ~/.kdcut/config.toml.

[video]
# One line on what this episode is. The AI uses it to judge what matters.
topic = ""
series = ""
episode = ""

[channel]
# style_notes = "Keep the jokes. Cut inventory management. I say 'chat' a lot, that's fine."
"""

USER_TEMPLATE = """\
# kdcut user settings. Edit paths to match your PC.

[channel]
name = "KDPlays"
style_notes = ""

[llm]
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"

[whisper]
model = "large-v3"
device = "auto"
mic_track = "auto"

[assets]
# Use forward slashes; they work on Windows and avoid escaping problems.
packs = [
  # "C:/Users/you/Downloads/Some Editing Pack",
]
"""
