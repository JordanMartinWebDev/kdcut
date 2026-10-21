"""Timelapses: silent clips (Flashback renders) that play as scenes of their own.

Unlike B-roll, which covers the gameplay while you keep talking, a timelapse is spliced into
the story: you say "alright, let's get started on this house", the video dips into the
timelapse, your voiceover (recorded afterwards) plays over it with the music, and when it's
done the video dips back into the gameplay.

Voiceovers are plain audio files dropped in next to the timelapses. A voiceover with the
same name as a timelapse (House.mp4 + House.m4a) belongs to it; otherwise the AI matches
them by what you say. Each voiceover is tidied like the main edit: long pauses and anything
after "cut that" come out, and it's levelled like your mic.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from kdcut import media
from kdcut.blocks import Block, fmt_ts

LEAD = 0.6          # seconds of timelapse before the voiceover starts
TAIL = 1.2          # seconds after it ends, before cutting back to the gameplay
FADE = 0.25         # dip to black at each end
MIN_SPEED = 0.5     # how far a timelapse may be slowed to fit a long voiceover


@dataclass
class Timelapse:
    path: Path
    duration: float
    voiceover: Path | None = None
    vo_words: list = field(default_factory=list)
    reason: str = ""

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Prepared:
    """A timelapse ready for the timeline."""
    item: Timelapse
    file: Path                      # rendered clip at timeline size/fps (silent)
    frames: int
    pieces: list[tuple[int, int, int]] = field(default_factory=list)   # (src_in, dur, offset) frames
    voice_file: Path | None = None  # levelled voiceover the pieces come from
    voice_seconds: float = 0.0
    note: str = ""


# --------------------------------------------------------------------------------------
# Files: which are timelapses, which are voiceovers, and which belong together
# --------------------------------------------------------------------------------------

def split_files(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """(timelapse videos, voiceover audio) from one mixed list."""
    vids = [Path(f) for f in files if Path(f).suffix.lower() in media.VIDEO_EXTS]
    auds = [Path(f) for f in files if Path(f).suffix.lower() in media.AUDIO_EXTS]
    return vids, auds


def _key(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", Path(name).stem.lower())
    s = re.sub(r"\b(voice ?over|vo|narration|audio|timelapse|time lapse|video)\b", " ", s)
    return " ".join(s.split())


def pair_by_name(timelapses: list[Timelapse], audio: list[Path]) -> list[Path]:
    """Give each timelapse the voiceover with the same name. Returns the audio left over."""
    left = list(audio)
    for t in timelapses:
        k = _key(t.name)
        hit = next((a for a in left if _key(a.name) == k), None) if k else None
        if hit is not None:
            t.voiceover = hit
            left.remove(hit)
    return left


# --------------------------------------------------------------------------------------
# Where each timelapse goes (and which loose voiceover it gets)
# --------------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "placements": {
            "type": "array",
            "description": "One entry per timelapse.",
            "items": {"type": "object", "properties": {
                "file": {"type": "string", "description": "Timelapse file name, exactly as listed."},
                "after": {"type": "string",
                          "description": "Block id the timelapse plays right after, or 'end' if it "
                                         "fits nowhere."},
                "voiceover": {"type": "string",
                              "description": "File name of the loose voiceover that narrates this "
                                             "timelapse, or '' for none."},
                "reason": {"type": "string"},
            }, "required": ["file", "after", "reason"]},
        },
    },
    "required": ["placements"],
}

RULES = (
    "These timelapses are silent clips (sped-up building) that play as their own scene: the "
    "gameplay cuts away to the timelapse, then comes back. A timelapse goes right after the "
    "creator has finished setting up that build: after he explains what he's going to build "
    "and why, at his cue to start ('let's go', 'let's get started', 'time to build'). Never "
    "before he's done explaining it: the viewer must hear the plan before seeing it built. "
    "If he lays out several builds together and then says 'let's go', their timelapses play "
    "there back to back, in the order he announced them. Lines marked [BUILD CUE] ('let's build "
    "this thing', 'cue the music', 'let's queue a time lapse') are his cue: the timelapse for "
    "that build goes right after the cue. A timelapse of something finished or "
    "a reveal goes after the line that leads into showing it. Use the file names and "
    "voiceovers to tell what each timelapse shows. Never put one in the middle of a sentence "
    "or a story.\n"
    "Loose voiceovers (no matching file name) were recorded afterwards to talk over a "
    "timelapse: give each one to the timelapse it describes, or to none."
)


def cache_key(kept: list[Block], timelapses: list[Timelapse], loose: list[tuple[str, str]]) -> str:
    sig = json.dumps(["v3", [b.id for b in kept], [(t.name, round(t.duration, 1), t.voiceover.name if t.voiceover else "")
                                            for t in timelapses], loose])
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]


def place(cfg: dict, video: dict, kept: list[Block], timelapses: list[Timelapse],
          loose: list[tuple[str, str]], caller) -> list[dict]:
    """Ask the AI where each timelapse goes. `loose` is [(voiceover file name, transcript)]."""
    lines = [f"{b.id}{' [BUILD CUE]' if 'build_cue' in b.flags else ''} {b.text}" for b in kept]
    tls = []
    for t in timelapses:
        line = f"- {t.name} ({t.duration:.0f}s)"
        if t.voiceover is not None:
            text = " ".join(w.text.strip() for w in t.vo_words)
            line += f"  voiceover: \"{text[:600]}\""
        tls.append(line)
    user = ""
    if video.get("topic"):
        user += f"Video topic: {video['topic']}\n"
    user += RULES + "\n\nTimelapses:\n" + "\n".join(tls) + "\n"
    if loose:
        user += "\nLoose voiceovers:\n" + "\n".join(f"- {n}: \"{t[:600]}\"" for n, t in loose) + "\n"
    user += "\nFinal edit transcript:\n" + "\n".join(lines)
    system = ("You are the editor of a Minecraft let's play. You decide where timelapse clips "
              "go in the finished story.")
    res = caller(cfg, system, user, "place_timelapses", "Say where each timelapse goes.", SCHEMA)
    return [p for p in res.get("placements", []) if isinstance(p, dict)]


def resolve(placements: list[dict], timelapses: list[Timelapse], loose: list[Path],
            block_ids: set[str]) -> tuple[dict[str, list[Timelapse]], list[str]]:
    """Turn the AI's answer into block id -> timelapses (in order). Anything the AI skipped
    goes at the end; loose voiceovers it didn't match are paired in order when the counts
    line up. Returns (inserts, review notes)."""
    by_name = {t.name: t for t in timelapses}
    loose_by_name = {p.name: p for p in loose}
    out: dict[str, list[Timelapse]] = {}
    notes: list[str] = []
    done: set[str] = set()
    for p in placements:
        t = by_name.get(str(p.get("file", "")))
        if t is None or t.name in done:
            continue
        done.add(t.name)
        t.reason = str(p.get("reason") or "")
        vo = loose_by_name.pop(str(p.get("voiceover") or ""), None)
        if vo is not None and t.voiceover is None:
            t.voiceover = vo
        after = str(p.get("after") or "")
        out.setdefault(after if after in block_ids else "__end__", []).append(t)
    missing = [t for t in timelapses if t.name not in done]
    for t in missing:
        out.setdefault("__end__", []).append(t)
        notes.append(f"{t.name}: no spot found, so it's at the end of the story; drag it where it belongs")
    no_vo = [t for t in timelapses if t.voiceover is None]
    left = list(loose_by_name.values())
    if left and len(left) == len(no_vo):
        for t, vo in zip(no_vo, sorted(left, key=lambda p: media._natural_key(p))):
            t.voiceover = vo
        left = []
    for vo in left:
        notes.append(f"Voiceover {vo.name} wasn't matched to a timelapse, so it's not in the video")
    return out, notes


def place_without_ai(timelapses: list[Timelapse], loose: list[Path]) -> tuple[dict[str, list[Timelapse]], list[str]]:
    inserts, notes = resolve([], timelapses, loose, set())
    notes = [n for n in notes if "no spot found" not in n]
    if timelapses:
        notes.insert(0, "Quick edit: timelapses are at the end of the story; drag them where they belong")
    return inserts, notes


# --------------------------------------------------------------------------------------
# Voiceover tidy-up and fitting the timelapse around it
# --------------------------------------------------------------------------------------

def voice_ranges(words: list, duration: float, edit: dict, voice=None) -> list[tuple[float, float]]:
    """The parts of a voiceover to keep: speech, with long pauses and 'cut that' taken out."""
    from kdcut import blocks as blocks_mod
    from kdcut.planner import plan_without_ai
    from kdcut.timeline import kept_ranges
    if not words:
        return []
    blocks = blocks_mod.build_blocks([words], gap=float(edit.get("block_gap", 0.8)),
                                     max_seconds=float(edit.get("max_block_seconds", 20.0)))
    ed = {**edit, "keep_pauses_up_to": max(float(edit.get("keep_pauses_up_to", 0.6)), 1.0)}
    ranges = kept_ranges(blocks, plan_without_ai(blocks), [duration], ed, {0: voice} if voice else None)
    return [(r.start, r.end) for r in ranges]


def fit(natural: float, voice_seconds: float) -> tuple[float, float, float]:
    """(speed, seconds held on the last frame, total seconds) so the voiceover fits.
    A timelapse longer than its voiceover plays as recorded (the rest is music); a shorter
    one is slowed down (to half speed at most), then holds its last frame."""
    if voice_seconds <= 0:
        return 1.0, 0.0, natural
    need = LEAD + voice_seconds + TAIL
    if natural >= need:
        return 1.0, 0.0, natural
    speed = max(natural / need, MIN_SPEED)
    played = natural / speed
    return round(speed, 3), round(max(need - played, 0.0), 3), round(max(need, played), 3)


def render(src: Path, dest: Path, fps: Fraction, width: int, height: int, speed: float, hold: float,
           total: float, transition: str = "dip", log=None) -> Path:
    """The timelapse at timeline size and frame rate, retimed if needed, with a quick dip to
    black at each end (transition='dip') or plain cuts ('cut'). Silent."""
    vf = [f"scale={width}:{height}:force_original_aspect_ratio=decrease",
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    if abs(speed - 1.0) > 1e-3:
        vf.append(f"setpts=PTS/{speed}")
    vf.append(f"fps={fps}")
    if hold > 0:
        vf.append(f"tpad=stop_mode=clone:stop_duration={hold:.3f}")
    if transition == "dip":
        vf += [f"fade=t=in:st=0:d={FADE}", f"fade=t=out:st={max(total - FADE, 0):.3f}:d={FADE}"]
    vf.append("format=yuv420p")
    frames = int(round(total * float(fps)))
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", ",".join(vf), "-an",
           "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(dest)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = media.run_watched(cmd, total, label=f"Preparing timelapse {src.name}", log=log, stall=60,
                             timeout=1800)
    if proc.returncode != 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise media.MediaError((proc.stderr.strip().splitlines() or ["ffmpeg failed"])[0][:300])
    media._PROBE_CACHE.pop(dest, None)
    return dest


def prepare(item: Timelapse, n: int, fps: Fraction, width: int, height: int, edit: dict, work: Path,
            log=print) -> Prepared:
    """Tidy the voiceover, fit and render the timelapse. Re-uses an earlier render."""
    from kdcut import audioprep, vad
    voice_file, ranges, vo_secs = None, [], 0.0
    if item.voiceover is not None:
        info = media.probe(item.voiceover)
        voice = vad.analyse(item.voiceover, 0, work, log=log)
        ranges = voice_ranges(item.vo_words, info.duration, edit, voice)
        if ranges:
            stems = audioprep.split_tracks(item.voiceover, info, 0, work / "audio", log=log)
            voice_file = stems[0].path if stems else item.voiceover
            vo_secs = sum(b - a for a, b in ranges)
    speed, hold, total = fit(item.duration, vo_secs)
    transition = str(edit.get("timelapse_transition", "dip"))
    st = item.path.stat()
    sig = f"{item.path}|{st.st_size}|{int(st.st_mtime)}|{fps}|{width}x{height}|{speed}|{hold}|{total}|{transition}"
    dest = work / "timelapses" / f"timelapse_{n}_{hashlib.sha1(sig.encode()).hexdigest()[:10]}.mp4"
    if not dest.is_file():
        log(f"Preparing timelapse {item.name}")
        render(item.path, dest, fps, width, height, speed, hold, total, transition, log=log)
    frames = int(round(total * float(fps)))

    def fr(sec: float) -> int:
        return int(round(sec * float(fps)))

    pieces, at = [], fr(LEAD)
    for a, b in ranges:
        src_in, dur = fr(a), fr(b) - fr(a)
        if dur <= 0:
            continue
        dur = min(dur, frames - at)
        if dur <= 0:
            break
        pieces.append((src_in, dur, at))
        at += dur
    notes = []
    if speed < 1:
        notes.append(f"slowed to {speed:.2f}x to fit the voiceover")
    if hold > 0.05:
        notes.append(f"held the last frame {hold:.1f}s; the voiceover is longer than the timelapse")
    return Prepared(item, dest, frames, pieces, voice_file, vo_secs, "; ".join(notes))


def review_line(at: float, seconds: float, p: Prepared, reason: str = "") -> str:
    vo = (f"voiceover {p.item.voiceover.name} ({p.voice_seconds:.0f}s)" if p.voice_file else "no voiceover")
    line = f"{fmt_ts(at)}  Timelapse  {p.item.name} ({seconds:.0f}s, {vo})"
    if reason:
        line += f"  ({reason})"
    if p.note:
        line += f"  [{p.note}]"
    return line
