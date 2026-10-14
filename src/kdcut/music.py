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
    genre: str = ""          # Epidemic Sound writes these into the file's tags
    mood: str = ""
    energy: str = ""
    bpm: str = ""
    sfx: bool = False        # a sound effect or ambience, not background music

    @property
    def name(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title

    @property
    def words(self) -> set[str]:
        text = " ".join(self.tags + [self.title, self.genre, self.mood, self.energy])
        return set(re.findall(r"[a-z]+", text.lower()))


INDEX_VERSION = 2


def parse_name(stem: str) -> tuple[str, str]:
    """'ES_Flying Over Hills - Hallman' -> ('Flying Over Hills', 'Hallman')."""
    s = re.sub(r"^ES[_ ]+", "", stem).strip()
    s = re.sub(r"\s*\((?:\d+|instrumental version|main version)\)$", "", s, flags=re.I)
    s = re.sub(r"(?<=\S)\(\d+\)$", "", s).strip()          # 'Beater Head(1)'
    if " - " in s:
        title, artist = s.rsplit(" - ", 1)
        return title.strip(), artist.strip()
    return s.replace("_", " ").strip(), ""


def looks_like_sfx(artist: str, duration: float, genre: str, mood: str, bpm: str, folders: list[str]) -> bool:
    """Epidemic sound effects are credited to 'SFX Producer' and carry no mood or tempo.
    Short clips are stingers/effects too. Anything in a folder called SFX counts."""
    if "sfx" in artist.lower() or any(re.search(r"\b(sfx|sound ?effects?|foley)\b", f.lower()) for f in folders):
        return True
    if duration < 20:
        return True
    return bool(genre) and not mood and not bpm     # e.g. 'Land Birds' ambience


def _copy_rank(path: str) -> tuple[int, int, str]:
    """Prefer 'Song.mp3' over 'Song (1).mp3' and 'Song_copy.mp3' when both exist."""
    stem = Path(path).stem
    copy = bool(re.search(r"\(\d+\)$|copy$", stem, flags=re.I))
    return (int(copy), len(stem), path)


@dataclass
class Library:
    music: list[Track] = field(default_factory=list)
    sfx: list[Track] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)   # extra copies that were ignored


def scan_library(folders: list[str | Path], cache_file: Path | None = None, log=print) -> Library:
    """Index music folders: background music, sound effects, and duplicate copies.
    Tags and durations are cached so re-scans are instant."""
    cache_file = cache_file or (HOME_DIR / "music_index.json")
    cached: dict[str, dict] = {}
    if cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == INDEX_VERSION:
                cached = {t["path"]: t for t in data["tracks"]}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            cached = {}
    found: list[Track] = []
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
                try:
                    found.append(Track(**c))
                    continue
                except TypeError:
                    pass
            try:
                info = media.probe(p)
            except media.MediaError:
                continue
            t = info.tags
            title, artist = parse_name(p.stem)
            title = t.get("title", "").strip() or title
            artist = t.get("artist", "").strip() or artist
            folders_ = [part for part in p.relative_to(root).parent.parts if part]
            genre = t.get("genre", "").strip()
            mood = (t.get("grouping") or t.get("tit1") or t.get("mood") or "").strip()
            energy = (t.get("tit3") or t.get("energy") or "").strip()
            bpm = (t.get("tbpm") or t.get("bpm") or "").strip()
            sfx = looks_like_sfx(artist, info.duration, genre, mood, bpm, folders_)
            found.append(Track(str(p), title, artist, folders_, round(info.duration, 1), mtime,
                               genre, mood, energy, bpm, sfx))
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"version": INDEX_VERSION, "tracks": [asdict(t) for t in found]}),
                              encoding="utf-8")
    except OSError:
        pass

    lib = Library()
    groups: dict[tuple, list[Track]] = {}
    for t in found:
        key = (t.title.lower(), t.artist.lower(), round(t.duration))
        groups.setdefault(key, []).append(t)
    for copies in groups.values():
        copies.sort(key=lambda t: _copy_rank(t.path))
        keep = copies[0]
        lib.duplicates += [c.path for c in copies[1:]]
        (lib.sfx if keep.sfx else lib.music).append(keep)
    order = {t.path: i for i, t in enumerate(found)}
    lib.music.sort(key=lambda t: order[t.path])
    lib.sfx.sort(key=lambda t: order[t.path])
    return lib


def scan(folders: list[str | Path], cache_file: Path | None = None, log=print) -> list[Track]:
    """Just the background music (sound effects and duplicate copies left out)."""
    return scan_library(folders, cache_file, log).music


def sfx_assets(sfx: list[Track]) -> list:
    """Sound effects found in the music folders, in the same shape as editing-pack assets,
    so the AI's sound-effect picks can use them."""
    from kdcut.assets import Asset, _singular, _tokenize
    out = []
    for t in sfx:
        toks = _tokenize(t.title) + _tokenize(t.genre.replace(",", " ")) + [x for f in t.tags for x in _tokenize(f)]
        toks = list(dict.fromkeys(_singular(x) for x in toks if not x.isdigit()))
        out.append(Asset(path=t.path, category="sfx", kind="audio", tokens=toks, name=t.title))
    return out


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
        about = [", ".join(t.tags)] if t.tags else []
        if t.mood:
            about.append(t.mood)
        if t.energy:
            about.append(f"{t.energy.lower()} energy")
        if t.genre:
            about.append(t.genre)
        if t.bpm:
            about.append(f"{t.bpm} bpm")
        desc = f" [{'; '.join(about)}]" if about else ""
        recent = " (used recently)" if history.get(t.path, 0) > week else ""
        lines.append(f"- {t.name}{desc} {int(t.duration // 60)}:{int(t.duration % 60):02d}{recent}")
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
    words = track.words
    return sorted(pool, key=lambda t: -len(words & t.words))


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


# --------------------------------------------------------------------------------------
# "Do I have enough music?" — coverage by the moods a Minecraft video needs
# --------------------------------------------------------------------------------------

# (name, what it's for, words that count, words that rule it out)
MOODS = [
    ("Calm", "building, farming, chill chatting",
     {"peaceful", "laid", "relaxing", "dreamy", "floating", "lullaby", "ambient", "lofi", "calm", "chill",
      "cozy", "acoustic", "piano", "low"}, {"dark", "suspense", "chasing", "epic", "angry", "scary"}),
    ("Adventure", "exploring, travelling, new biomes",
     {"adventure", "fantasy", "folk", "countryside", "celtic", "medieval", "orchestral", "acoustic",
      "exploring", "journey"}, {"dark", "scary"}),
    ("Upbeat", "intros, montages, good news",
     {"happy", "euphoric", "pop", "beats", "disco", "funk", "house", "party", "electro", "glamorous"},
     {"sad", "dark", "low"}),
    ("Funny", "fails, silly moments, bits",
     {"funny", "quirky", "eccentric", "silly", "cartoon", "comedy", "playful", "sneaky"}, set()),
    ("Tense", "caves, the Nether, low health, night",
     {"dark", "mysterious", "suspense", "scary", "tense", "weird", "horror", "thriller", "mystery", "creepy",
      "eerie", "drone"}, set()),
    ("Action", "fights, boss battles, raids, escapes",
     {"epic", "chasing", "heavy", "angry", "aggressive", "action", "trailer", "battle", "busy", "frantic"},
     {"low", "peaceful", "laid"}),
    ("Emotional", "big moments, endings, losses",
     {"sad", "sentimental", "melancholic", "nostalgic", "heartfelt"}, {"high"}),
]
ENOUGH = 8      # about four episodes before a mood has to repeat a track within the same week
THIN = 4


def mood_buckets(track: Track) -> list[str]:
    w = track.words
    if not (track.mood or track.genre):
        return []
    return [name for name, _, want, avoid in MOODS if w & want and not w & avoid]


@dataclass
class Coverage:
    mood: str
    use: str
    tracks: list[Track]

    @property
    def status(self) -> str:
        n = len(self.tracks)
        return "good" if n >= ENOUGH else "thin" if n >= THIN else "missing"


def coverage(lib: Library) -> tuple[list[Coverage], list[Track]]:
    """Tracks per mood (a track can count for several), plus the untagged ones."""
    rows = [Coverage(name, use, []) for name, use, _, _ in MOODS]
    by = {c.mood: c for c in rows}
    untagged = []
    for t in lib.music:
        b = mood_buckets(t)
        if not b and not (t.mood or t.genre):
            untagged.append(t)
        for name in b:
            by[name].tracks.append(t)
    return rows, untagged


SEARCH_TIPS = {
    "Calm": "Epidemic Sound > Music > Moods: Peaceful / Laid Back, Energy: Low (try 'acoustic', 'lofi', 'ambient')",
    "Adventure": "Genres: Adventure, Fantasy & Dreamy, Folk; Moods: Hopeful, Restless",
    "Upbeat": "Moods: Happy, Euphoric; Genres: Pop, Beats, Funk",
    "Funny": "Moods: Quirky, Eccentric, Sneaky; Genre: Quirky & Funny",
    "Tense": "Moods: Mysterious, Dark, Suspense; Genres: Mystery, Horror, Drone",
    "Action": "Moods: Epic, Chasing; Genres: Action, Trailer, Hybrid Orchestral",
    "Emotional": "Moods: Sad, Sentimental, Hopeful; Genre: Cinematic / Piano",
}


def report(lib: Library) -> str:
    rows, untagged = coverage(lib)
    mark = {"good": "OK  ", "thin": "THIN", "missing": "NEED"}
    out = [f"{len(lib.music)} music tracks, {len(lib.sfx)} sound effects"
           + (f", {len(lib.duplicates)} duplicate copies ignored" if lib.duplicates else ""), ""]
    for c in rows:
        out.append(f"{mark[c.status]}  {c.mood:<10} {len(c.tracks):>3}  ({c.use})")
    short = [c for c in rows if c.status != "good"]
    if short:
        out += ["", f"Worth adding (aim for {ENOUGH}+ each so episodes don't repeat):"]
        for c in short:
            out.append(f"- {c.mood}: {ENOUGH - len(c.tracks)} more. {SEARCH_TIPS[c.mood]}")
    if untagged:
        out += ["", f"{len(untagged)} track(s) have no mood tags, so the AI only sees their names. "
                    "Put them in mood folders (Calm/, Tense/...) to help it pick:"]
        out += [f"- {t.name}" for t in untagged[:20]]
    if lib.sfx:
        out += ["", "Sound effects found here (used for SFX, never as music): "
                + ", ".join(t.title for t in lib.sfx[:30])]
    if lib.duplicates:
        out += ["", "Duplicate copies (safe to delete):"] + [f"- {Path(p).name}" for p in lib.duplicates]
    return "\n".join(out)
