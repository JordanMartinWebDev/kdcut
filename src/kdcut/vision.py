"""Let the AI look at the footage, cheaply.

The transcript says what you *said*. This module finds where something might be happening
that you *didn't* talk about, grabs a few small frames there, and asks Claude to describe
them. The edit planner then sees lines like

    [on screen, 24s without talking] 0:12 fighting zombies at night, low health (action 3)

so it can keep a wordless fight and cut an AFK stretch.

Where to look comes from free local checks first (no AI cost):
  * stretches of the recording where you aren't talking
  * loud moments in the game audio (explosions, hits, mobs)
  * scene changes (teleports, deaths, entering the Nether)
  * moments you flagged with "clip that"
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kdcut import llm, media
from kdcut.blocks import Block, fmt_ts

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------------------
# Free local signals
# --------------------------------------------------------------------------------------

def loudness(path: Path, track: int, window: float = 0.5) -> list[tuple[float, float]]:
    """(time, RMS dB) every `window` seconds for one audio track."""
    n = int(8000 * window)
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{track}", "-af",
           f"aresample=8000,asetnsamples=n={n},astats=metadata=1:reset=1,"
           "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
           "-f", "null", "-"]
    try:
        out = media.run_quiet(cmd, timeout=3600).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    result, t = [], None
    for line in out.splitlines():
        if "pts_time:" in line:
            t = float(line.rsplit("pts_time:", 1)[1].split()[0])
        elif line.startswith("lavfi.astats.Overall.RMS_level=") and t is not None:
            v = line.split("=", 1)[1]
            result.append((t, -120.0 if "inf" in v else float(v)))
    return result


def loud_moments(levels: list[tuple[float, float]], above_median_db: float = 12.0) -> list[float]:
    vals = sorted(v for _, v in levels if v > -100)
    if not vals:
        return []
    median = vals[len(vals) // 2]
    return [t for t, v in levels if v >= median + above_median_db]


def scene_changes(path: Path, threshold: float = 0.3) -> list[float]:
    """Big visual changes, checked on keyframes only so a 2-hour file takes seconds."""
    cmd = ["ffmpeg", "-hide_banner", "-skip_frame", "nokey", "-i", str(path), "-an", "-vf",
           f"scale=160:-2,select='gt(scene,{threshold})',metadata=mode=print:file=-",
           "-f", "null", "-"]
    try:
        out = media.run_quiet(cmd, timeout=3600).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", out)]


def game_audio_track(info: media.MediaInfo, mic_setting) -> int | None:
    """Prefer a track that isn't the mic. With a single mixed track we still use it: inside
    the stretches we look at, you aren't talking, so loud sounds there are the game."""
    if not info.audio:
        return None
    for a in info.audio:
        if any(k in a.title.lower() for k in ("game", "desktop")):
            return a.index
    if len(info.audio) == 1:
        return 0
    mic = media.pick_mic_track(info, mic_setting)
    return 0 if mic != 0 else 1


# --------------------------------------------------------------------------------------
# Choosing where to look
# --------------------------------------------------------------------------------------

@dataclass
class Look:
    source: int
    time: float
    after_block: str | None   # the speech block right before this moment
    gap: float                # length of the wordless stretch it belongs to (0 for highlights)
    why: str
    desc: str = ""
    action: int = -1
    id: str = ""


@dataclass
class Gap:
    source: int
    start: float
    end: float
    after_block: str | None
    loud: list[float] = field(default_factory=list)
    scenes: list[float] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start


def find_gaps(blocks: list[Block], durations: list[float], min_gap: float) -> list[Gap]:
    gaps: list[Gap] = []
    by_src: dict[int, list[Block]] = {}
    for b in blocks:
        by_src.setdefault(b.source, []).append(b)
    for src, dur in enumerate(durations):
        bl = by_src.get(src, [])
        edges = [(None, 0.0)] + [(b.id, b.end) for b in bl]
        starts = [b.start for b in bl] + [dur]
        for (after, gap_start), gap_end in zip(edges, starts):
            if gap_end - gap_start >= min_gap:
                gaps.append(Gap(src, gap_start, gap_end, after))
    return gaps


def plan_looks(blocks: list[Block], gaps: list[Gap], max_frames: int) -> list[Look]:
    looks: list[Look] = []
    for b in blocks:
        if "highlight" in b.flags:
            looks.append(Look(b.source, b.start + min(1.0, b.duration / 2), b.id, 0.0, "you said 'clip that'"))

    def priority(g: Gap) -> float:
        return len(g.loud) * 3 + len(g.scenes) * 2 + g.length / 10

    for g in sorted(gaps, key=priority, reverse=True):
        if len(looks) >= max_frames:
            break
        n = max(1, min(4, math.ceil(g.length / 12)))
        inner_start, inner_end = g.start + 1.0, g.end - 1.0
        if inner_end <= inner_start:
            continue
        # Interesting points first (loudest moments / scene changes), then even spacing.
        hot = sorted(set(round(t) for t in g.loud + g.scenes if inner_start <= t <= inner_end))
        times = hot[:n]
        step = (inner_end - inner_start) / (n + 1)
        k = 1
        while len(times) < n:
            t = inner_start + step * k
            if all(abs(t - x) > 3 for x in times):
                times.append(t)
            k += 1
            if k > n + 4:
                break
        why = "no talking"
        if g.loud:
            why += ", loud game audio"
        if g.scenes:
            why += ", scene change"
        for t in sorted(times)[: max_frames - len(looks)]:
            looks.append(Look(g.source, t, g.after_block, g.length, why))
    looks.sort(key=lambda lk: (lk.source, lk.time))
    for i, lk in enumerate(looks, 1):
        lk.id = f"F{i:03d}"
    return looks


# --------------------------------------------------------------------------------------
# Asking Claude what's on screen
# --------------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "desc": {"type": "string", "description": "What is happening, max 15 words."},
            "action": {"type": "integer", "minimum": 0, "maximum": 3,
                       "description": "0 = menu/inventory/loading/AFK/nothing; 1 = calm walking, "
                                      "mining or building; 2 = something happening (mob, discovery, "
                                      "travel with a view); 3 = intense, exciting or funny."},
        }, "required": ["id", "desc", "action"]}},
    },
    "required": ["frames"],
}

SYSTEM = ("You are helping edit a Minecraft let's-play. You see still frames from moments where "
          "the creator wasn't talking. For each frame say briefly what is happening in the game "
          "and how interesting it is to a viewer. Be concrete (mobs, items, places, health).")


def describe(looks: list[Look], recordings: list[Path], cfg: dict, frames_dir: Path,
             caller=llm.call_tool, log=print, check=lambda: None, batch: int = 16) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for lk in looks:
        img = frames_dir / f"{lk.id}.jpg"
        if not img.exists():
            try:
                media.extract_frame(recordings[lk.source], lk.time, img, width=512)
            except media.MediaError:
                continue
    todo = [lk for lk in looks if (frames_dir / f"{lk.id}.jpg").exists()]
    for i in range(0, len(todo), batch):
        check()
        chunk = todo[i:i + batch]
        log(f"Looking at the footage ({i + len(chunk)}/{len(todo)} frames)")
        content: list[dict] = []
        for lk in chunk:
            label = f"{lk.id}: recording {lk.source + 1} at {fmt_ts(lk.time)} ({lk.why})"
            content.append({"type": "text", "text": label})
            content.append(llm.image_block(frames_dir / f"{lk.id}.jpg", max_side=512))
        content.append({"type": "text", "text": "Describe every frame by calling the tool."})
        res = caller(cfg, SYSTEM, content, "describe_frames", "Describe the frames.", SCHEMA)
        by_id = {f.get("id"): f for f in res.get("frames", [])}
        for lk in chunk:
            f = by_id.get(lk.id)
            if f:
                lk.desc = str(f.get("desc", ""))[:160]
                lk.action = int(f.get("action", -1))


def screen_notes(looks: list[Look]) -> dict[str, list[str]]:
    """Block id -> lines to show after it in the transcript the planner reads."""
    notes: dict[str, list[str]] = {}
    for lk in looks:
        if not lk.desc or not lk.after_block:
            continue
        where = f"{lk.gap:.0f}s without talking" if lk.gap else "flagged moment"
        notes.setdefault(lk.after_block, []).append(
            f"[on screen, {where}] {fmt_ts(lk.time)} {lk.desc} (action {lk.action})")
    return notes


# --------------------------------------------------------------------------------------

def analyse(recordings: list[Path], infos: list[media.MediaInfo], blocks: list[Block], cfg: dict,
            work: Path, caller=llm.call_tool, log=print, check=lambda: None) -> dict[str, list[str]]:
    """Run the whole look-at-the-footage step. Cached in work/vision.json."""
    vc = cfg.get("vision", {})
    cache = work / "vision.json"
    key = {"recs": [[str(r), r.stat().st_size] for r in recordings],
           "blocks": len(blocks), "max": vc.get("max_frames", 120), "gap": vc.get("min_gap", 8)}
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key:
                log("Using earlier footage notes")
                return screen_notes([Look(**lk) for lk in data["looks"]])
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    gaps = find_gaps(blocks, [i.duration for i in infos], float(vc.get("min_gap", 8)))
    log(f"Checking game audio and scene changes in {len(gaps)} quiet stretch(es)")
    for src, (rec, info) in enumerate(zip(recordings, infos)):
        check()
        mine = [g for g in gaps if g.source == src]
        if not mine:
            continue
        track = game_audio_track(info, cfg["whisper"].get("mic_track", "auto"))
        loud = loud_moments(loudness(rec, track)) if track is not None else []
        scenes = scene_changes(rec) if info.has_video else []
        for g in mine:
            g.loud = [t for t in loud if g.start <= t <= g.end]
            g.scenes = [t for t in scenes if g.start <= t <= g.end]
    looks = plan_looks(blocks, gaps, int(vc.get("max_frames", 120)))
    if not looks:
        return {}
    describe(looks, recordings, cfg, work / "frames", caller=caller, log=log, check=check)
    cache.write_text(json.dumps({"key": key, "looks": [asdict(lk) for lk in looks]}, indent=1),
                     encoding="utf-8")
    return screen_notes(looks)
