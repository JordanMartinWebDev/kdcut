"""Background music from the creator's own licensed library (e.g. Epidemic Sound downloads).

KDcut never downloads music. You point it at folders of tracks you're licensed to use; it
indexes them, the AI picks a track per section of the video (calm for building, tense for a
cave, upbeat for the intro, or nothing at all), and the music lands on its own audio track
in Resolve at background volume.

Tip: organise the folder by mood (Calm/, Tense/, Upbeat/, Funny/...). Folder names and
Epidemic's "ES_Track Name - Artist" filenames are what the AI sees.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kdcut import media
from kdcut.config import HOME_DIR

MUSIC_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
HISTORY_FILE = HOME_DIR / "music_history.json"


@dataclass
class Track:
    path: str
    title: str
    artist: str
    tags: list[str]          # from folder names (your mood folders)
    duration: float
    mtime: int = 0

    @property
    def name(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title


def parse_name(stem: str) -> tuple[str, str]:
    """'ES_Flying Over Hills - Hallman' -> ('Flying Over Hills', 'Hallman')."""
    s = re.sub(r"^ES[_ ]+", "", stem).strip()
    s = re.sub(r"\s*\((?:\d+|instrumental version|main version)\)$", "", s, flags=re.I)
    if " - " in s:
        title, artist = s.rsplit(" - ", 1)
        return title.strip(), artist.strip()
    return s.replace("_", " ").strip(), ""


def scan(folders: list[str | Path], cache_file: Path | None = None, log=print) -> list[Track]:
    """Index music folders. Durations are cached so re-scans are instant."""
    cache_file = cache_file or (HOME_DIR / "music_index.json")
    cached: dict[str, dict] = {}
    if cache_file.is_file():
        try:
            cached = {t["path"]: t for t in json.loads(cache_file.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            cached = {}
    tracks: list[Track] = []
    for folder in folders:
        root = Path(folder).expanduser()
        if not root.is_dir():
            log(f"Music folder not found: {root}")
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in MUSIC_EXTS or p.name.startswith("._"):
                continue
            mtime = int(p.stat().st_mtime)
            c = cached.get(str(p))
            if c and c.get("mtime") == mtime:
                tracks.append(Track(**c))
                continue
            try:
                dur = media.probe(p).duration
            except media.MediaError:
                continue
            if dur < 20:          # stingers and SFX don't make background music
                continue
            title, artist = parse_name(p.stem)
            tags = [part for part in p.relative_to(root).parent.parts if part]
            tracks.append(Track(str(p), title, artist, tags, round(dur, 1), mtime))
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps([asdict(t) for t in tracks]), encoding="utf-8")
    except OSError:
        pass
    return tracks


# --------------------------------------------------------------------------------------
# Variety: remember what was used recently
# --------------------------------------------------------------------------------------

def load_history(path: Path | None = None) -> dict[str, float]:
    try:
        return json.loads((path or HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record_use(paths: list[str], path: Path | None = None) -> None:
    hist = load_history(path)
    now = time.time()
    for p in paths:
        hist[p] = now
    try:
        (path or HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
        (path or HISTORY_FILE).write_text(json.dumps(hist), encoding="utf-8")
    except OSError:
        pass


def library_text(tracks: list[Track], history: dict[str, float], limit: int = 150) -> str:
    """What the AI sees: one line per track, recently used ones marked."""
    week = time.time() - 7 * 86400
    lines = []
    for t in tracks[:limit]:
        tags = f" [{', '.join(t.tags)}]" if t.tags else ""
        recent = " (used recently)" if history.get(t.path, 0) > week else ""
        lines.append(f"- {t.name}{tags} {int(t.duration // 60)}:{int(t.duration % 60):02d}{recent}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Filling sections with music
# --------------------------------------------------------------------------------------

@dataclass
class Cue:
    start: float                     # seconds on the timeline
    end: float
    tracks: list[Track] = field(default_factory=list)   # preferred order; [] = silence
    why: str = ""


@dataclass
class Placement:
    track: Track
    start: float                     # timeline seconds
    offset: float                    # seconds into the track
    length: float


def _similar(track: Track, pool: list[Track]) -> list[Track]:
    words = set(" ".join(track.tags).lower().split()) | set(track.title.lower().split())
    return sorted(pool, key=lambda t: -len(words & (set(" ".join(t.tags).lower().split())
                                                    | set(t.title.lower().split()))))


def fill(cues: list[Cue], library: list[Track], history: dict[str, float],
         seed: int = 0, min_piece: float = 8.0) -> list[Placement]:
    """Turn cues into concrete clips. A section longer than its track continues with a
    similar track (never the same one twice in a row), avoiding recently used music."""
    rng = random.Random(seed)
    used: set[str] = set()
    out: list[Placement] = []
    week = time.time() - 7 * 86400
    for cue in cues:
        if not cue.tracks:
            continue
        t, prev = cue.start, None
        queue = list(cue.tracks)
        distinct = len({x.path for x in library} | {x.path for x in cue.tracks})
        guard = 0
        while cue.end - t >= min_piece and guard < 200:
            guard += 1
            if not queue:
                pool = [x for x in library if x.path not in used and (prev is None or x.path != prev.path)]
                fresh = [x for x in pool if history.get(x.path, 0) < week] or pool or library
                ranked = _similar(prev or cue.tracks[0], fresh)[:5]
                queue = [rng.choice(ranked)] if ranked else []
                if not queue:
                    break
            track = queue.pop(0)
            if prev is not None and track.path == prev.path and distinct > 1:
                continue
            length = min(track.duration, cue.end - t)
            if length < min_piece:
                break
            out.append(Placement(track, t, 0.0, length))
            used.add(track.path)
            prev, t = track, t + length
    return out


SCHEMA_PART = {
    "type": "array",
    "description": "Background music. Each cue starts at a block and runs until the next cue "
                   "(or the end). Use an empty track to leave a stretch without music.",
    "items": {"type": "object", "properties": {
        "at": {"type": "string", "description": "Block id where this music starts."},
        "track": {"type": "string", "description": "Exact track name from the library, or '' for silence."},
        "backup": {"type": "string", "description": "A second track in the same mood, for long sections."},
        "why": {"type": "string"},
    }, "required": ["at", "track"]},
}

RULES = (
    "Pick background music from the creator's library. Match the mood of each part of the video "
    "(calm/cozy for building and farming, adventurous for exploring, tense for caves, fights and "
    "low health, upbeat for the intro, playful for funny moments). Change music when the mood "
    "changes, usually at chapter boundaries, not more than every couple of minutes. Leave music "
    "out where silence is stronger (a scary moment, a big reveal, an emotional beat). Avoid "
    "tracks marked (used recently) so episodes don't all sound the same."
)
