"""Desktop-app logic that doesn't depend on Qt (so it can be tested anywhere)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kdcut import media

RECORDING_EXTS = media.VIDEO_EXTS
BROLL_EXTS = media.VIDEO_EXTS | media.IMAGE_EXTS
TIMELAPSE_EXTS = media.VIDEO_EXTS | media.AUDIO_EXTS
SCREENSHOT_EXTS = media.IMAGE_EXTS
SHORT_VIDEO_EXTS = media.VIDEO_EXTS
SHORT_AUDIO_EXTS = media.AUDIO_EXTS


def natural_key(p: str | Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(p).name)]


def collect_files(paths: list[str | Path], exts: set[str]) -> tuple[list[Path], list[Path]]:
    """Expand dropped files/folders into media files. Returns (accepted, rejected)."""
    accepted: list[Path] = []
    rejected: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found = sorted((f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts),
                           key=natural_key)
            accepted.extend(found)
        elif p.is_file() and p.suffix.lower() in exts:
            accepted.append(p)
        else:
            rejected.append(p)
    return accepted, rejected


def merge_unique(existing: list[Path], new: list[Path], sort: bool) -> list[Path]:
    seen = {str(p).lower() for p in existing}
    out = list(existing)
    for p in new:
        if str(p).lower() not in seen:
            seen.add(str(p).lower())
            out.append(p)
    return sorted(out, key=natural_key) if sort else out


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return cleaned or "Untitled video"


@dataclass
class Project:
    name: str = ""
    topic: str = ""
    recordings: list[str] = field(default_factory=list)
    broll: list[str] = field(default_factory=list)
    timelapses: list[str] = field(default_factory=list)   # timelapse videos + voiceover audio
    series: str = ""
    episode: str = ""
    screenshots: list[str] = field(default_factory=list)
    # The vertical Short: build timelapse(s), glamor shots of the finished build, a voiceover.
    short_timelapses: list[str] = field(default_factory=list)
    short_glamor: list[str] = field(default_factory=list)
    short_voiceover: list[str] = field(default_factory=list)
    short_title: str = ""

    def short_files(self):
        from kdcut.shorts import ShortFiles
        return ShortFiles(timelapses=[Path(p) for p in self.short_timelapses],
                          glamor=[Path(p) for p in self.short_glamor],
                          voiceover=Path(self.short_voiceover[0]) if self.short_voiceover else None,
                          title=self.short_title.strip())

    def out_dir(self, projects_dir: Path) -> Path:
        return Path(projects_dir) / safe_name(self.name)

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "project.json").write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, folder: Path) -> "Project | None":
        try:
            data = json.loads((Path(folder) / "project.json").read_text(encoding="utf-8"))
            return cls(**{k: data.get(k, v) for k, v in asdict(cls()).items()})
        except (OSError, json.JSONDecodeError, TypeError):
            return None


def validate(project: Project) -> list[str]:
    problems = []
    if not project.name.strip():
        problems.append("Give the video a name (it becomes the folder and timeline name).")
    if not project.recordings:
        problems.append("Drag at least one recording into the Recordings box.")
    if project.short_voiceover and not (project.short_timelapses or project.short_glamor):
        problems.append("The Short has a voiceover but no timelapse or glamor shots to show under it.")
    missing = [r for r in project.recordings + project.broll + project.timelapses + project.short_timelapses
               + project.short_glamor + project.short_voiceover if not Path(r).exists()]
    if missing:
        problems.append("These files can't be found anymore:\n" + "\n".join(missing[:5]))
    return problems


WINDOW_FILE = Path.home() / ".kdcut" / "window.json"


def load_window_geometry(path: Path | None = None) -> str:
    try:
        return str(json.loads((path or WINDOW_FILE).read_text(encoding="utf-8")).get("geometry") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def save_window_geometry(geometry: str, path: Path | None = None) -> None:
    path = path or WINDOW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"geometry": geometry}), encoding="utf-8")


def known_series(cfg: dict) -> list[str]:
    """Series that already have a thumbnail style saved."""
    d = Path(cfg["thumbnails"]["themes_dir"]).expanduser()
    names = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            names.append(json.loads(f.read_text(encoding="utf-8")).get("name") or f.stem)
        except (OSError, json.JSONDecodeError):
            pass
    return names


def setup_problems(cfg: dict) -> list[str]:
    from kdcut.llm import api_key
    out = []
    if not api_key(cfg):
        out.append("Add your Claude API key (needed for the AI edit).")
    if not cfg["assets"]["packs"]:
        out.append("Add your editing pack folders (for sound effects and overlays).")
    elif not Path(cfg["assets"]["index_path"]).expanduser().exists():
        out.append("Index your editing packs.")
    return out


def bundle_dir() -> Path | None:
    """Folder with files bundled into the installed app (None when running from source)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def use_bundled_ffmpeg() -> None:
    """Put the ffmpeg that ships with the app first on PATH."""
    b = bundle_dir()
    if b and (b / "ffmpeg").is_dir():
        os.environ["PATH"] = str(b / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")


def ffmpeg_ok() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def summary_text(result: dict) -> str:
    from kdcut.blocks import fmt_ts
    p = result["placed"]
    raw, final = result["raw_seconds"], result["final_seconds"]
    pct = f" ({100 * final / raw:.0f}% kept)" if raw else ""
    return (f"{fmt_ts(raw)} of footage became a {fmt_ts(final)} video{pct}.\n"
            + (f"Added {p['timelapse']} timelapse(s), " if p.get("timelapse") else "Added ")
            + (f"{p['fast']} fast-forward(s), " if p.get("fast") else "")
            + f"{p['broll']} B-roll clip(s), {p['overlay']} overlay(s), "
            f"{p['sfx']} sound effect(s) and {p.get('music', 0)} music track(s)."
            + short_text(result.get("short")))


def short_text(short: dict | None) -> str:
    if not short:
        return ""
    if short.get("error"):
        return f"\nThe Short couldn't be made: {short['error']}"
    return f"\nMade a {short['seconds']:.0f}-second Short with captions."


RESOLVE_STEPS = (
    "1. Open DaVinci Resolve and your project (or make a new one).\n"
    "2. File > Import > Timeline...\n"
    "3. Pick the .fcpxml file in this video's folder.\n"
    "4. If Resolve asks about settings, keep the timeline's settings.\n\n"
    "Your voice is on A1 and game audio on A2, each levelled (a one-track recording is all on A1). "
    "Voiceovers for timelapses are on A1 too, under their timelapse. "
    "Music is on A3 and sound effects below it. B-roll is on V2, overlays on V3. Chapter markers are on the "
    "timeline. If there's a cold open, it's the first clip; delete it if you don't like it. "
    "Check review.md for what was cut."
)
