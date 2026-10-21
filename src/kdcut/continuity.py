"""The viewer pass: watch the finished edit like a first-time viewer, before it's built.

The editing pass decides cuts window by window, and each choice can look fine on its own
while the story breaks between them: "And as for bookshelves ... also check" with the
bookshelf part cut, "get my stuff back" after the death was cut, a build timelapse playing
before the plan that sets it up. This pass reads the edit the way it will play (kept lines,
what was cut and why, what's on screen, where timelapses and fast-forwards land) and fixes
what a viewer would trip over: it brings cut lines back and moves timelapses. It never cuts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kdcut.blocks import Block, fmt_ts

VERSION = 2

SCHEMA = {
    "type": "object",
    "properties": {
        "restore": {
            "type": "array",
            "description": "Cut lines to bring back, because the viewer needs them to follow.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "why": {"type": "string", "description": "What breaks without it, under 15 words."},
            }, "required": ["id", "why"]},
        },
        "move_timelapse": {
            "type": "array",
            "description": "Timelapses in the wrong spot, and the block they should play right after.",
            "items": {"type": "object", "properties": {
                "file": {"type": "string"},
                "after": {"type": "string"},
                "why": {"type": "string"},
            }, "required": ["file", "after", "why"]},
        },
        "problems": {
            "type": "array",
            "description": "Anything else a viewer would trip over that the two fixes above can't solve.",
            "items": {"type": "object", "properties": {
                "at": {"type": "string", "description": "Block id."},
                "what": {"type": "string"},
            }, "required": ["at", "what"]},
        },
    },
    "required": ["restore", "move_timelapse"],
}

SYSTEM = (
    "You check a Minecraft let's-play edit before it's built, as a first-time viewer would watch "
    "it. The creator records short clips on purpose and narrates for the edit, so his words are "
    "hard clues to the story. Kept lines play in order; lines marked CUT are not in the video. "
    "Find every spot where the viewer would lose the story, and fix it:\n"
    "- A kept line refers to something the viewer never saw or heard because it was cut ('get my "
    "stuff back' when the death was cut; 'also check' when 'as for bookshelves' was cut): "
    "restore the cut lines it needs.\n"
    "- A checklist or a sentence spread over several clips is partly cut: restore the rest.\n"
    "- A [on screen] note shows an event the story depends on (a death, a discovery) and the "
    "line about it was cut: restore it.\n"
    "- A timelapse plays before the talk that sets it up (the plan, the 'why'), or before the "
    "creator's cue to start ('let's go'): move it right after the last line that leads into it. "
    "Builds announced together play together, in the order he announced them. Lines marked "
    "[BUILD CUE] are his cue that the work starts: a timelapse for that build goes right after.\n"
    "- A build is shown but its payoff is cut (the 'it's done' line, the test, the result): "
    "restore it.\n"
    "Only restore what the story needs; leave cuts of real waste (retakes, rambling, dead ends) "
    "alone. Don't invent problems: if the edit flows, return empty lists."
)


@dataclass
class Fixes:
    restore: list[dict] = field(default_factory=list)
    moves: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def script(blocks: list[Block], dropped: dict[str, str], durations: list[float],
           notes: dict[str, list[str]] | None = None, inserts: dict[str, list[str]] | None = None) -> str:
    """The edit as it will play: kept lines, CUT lines with the reason, what's on screen, and
    what gets inserted after each line."""
    out, last_src = [], None
    for b in blocks:
        if b.source != last_src:
            length = f" ({durations[b.source]:.0f}s clip)" if b.source < len(durations) else ""
            out.append(f"--- Recording {b.source + 1}{length} ---")
            last_src = b.source
        if "forced_drop" in b.flags:
            out.append(f"{b.id} CUT (creator said cut) {b.text[:60]}")
            continue
        tag = (" [BUILD CUE]" if "build_cue" in b.flags else "") + (" [TIME SKIP]" if "time_skip" in b.flags else "")
        if b.id in dropped:
            out.append(f"{b.id} CUT ({dropped[b.id] or 'editor'}) [{fmt_ts(b.start)}]{tag} {b.text}")
        else:
            out.append(f"{b.id} [{fmt_ts(b.start)}]{tag} {b.text}")
        out += [f"      {n}" for n in (notes or {}).get(b.id, [])]
        out += [f"      >> {x}" for x in (inserts or {}).get(b.id, [])]
    return "\n".join(out)


def check(cfg: dict, video: dict, blocks: list[Block], dropped: dict[str, str], durations: list[float],
          caller, notes=None, inserts=None, outline_text: str = "", timelapse_files: list[str] = (),
          window: int = 600, log=print) -> Fixes:
    fixes = Fixes()
    ids = {b.id for b in blocks}
    windows = [blocks[i:i + window] for i in range(0, len(blocks), window)] or [[]]
    for n, chunk in enumerate(windows):
        if not chunk:
            continue
        if len(windows) > 1:
            log(f"Watching the edit as a viewer ({n + 1}/{len(windows)})")
        user = ""
        if video.get("topic"):
            user += f"What the creator says this episode is: {video['topic']}\n"
        if outline_text:
            user += "\nSTORY OUTLINE:\n" + outline_text + "\n"
        if timelapse_files:
            user += "\nTimelapse files: " + ", ".join(timelapse_files) + "\n"
        user += "\nThe edit:\n" + script(chunk, dropped, durations, notes, inserts)
        res = caller(cfg, SYSTEM, user, "submit_fixes", "Submit the fixes for the edit.", SCHEMA)
        for r in res.get("restore", []) or []:
            if isinstance(r, dict) and r.get("id") in ids and r["id"] in dropped:
                fixes.restore.append({"id": r["id"], "why": str(r.get("why", ""))[:100]})
        for m in res.get("move_timelapse", []) or []:
            if isinstance(m, dict) and m.get("after") in ids and m.get("file") in set(timelapse_files):
                fixes.moves.append({"file": m["file"], "after": m["after"], "why": str(m.get("why", ""))[:100]})
        for p in res.get("problems", []) or []:
            if isinstance(p, dict) and p.get("what"):
                fixes.problems.append(f"{p.get('at', '')}: {str(p['what'])[:160]}")
    return fixes


def apply(plan, fixes: Fixes, blocks: list[Block]) -> list[str]:
    """Bring back the lines the viewer needs. Returns the restored ids."""
    by_id = {b.id: b for b in blocks}
    restored = []
    for r in fixes.restore:
        b = by_id.get(r["id"])
        if b is None or "forced_drop" in b.flags or r["id"] not in plan.drop:
            continue
        plan.drop.pop(r["id"])
        restored.append(r["id"])
    return restored


def cache_key(plan, timelapse_spots: dict[str, str]) -> str:
    import hashlib
    import json
    sig = json.dumps([VERSION, plan.blocks_key, sorted(plan.drop), sorted(plan.extend),
                      sorted(timelapse_spots.items()), [f["id"] for f in plan.fast]])
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]
