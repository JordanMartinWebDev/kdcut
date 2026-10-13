"""The AI editor: reads the transcript and decides what to cut, keep and decorate."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from kdcut import llm
from kdcut.blocks import Block, fmt_ts


@dataclass
class Plan:
    drop: dict[str, str] = field(default_factory=dict)      # block id -> reason
    extend: dict[str, float] = field(default_factory=dict)  # block id -> seconds of silent gameplay to keep after it
    broll: list[dict] = field(default_factory=list)         # {file, at, duration, reason}
    overlays: list[dict] = field(default_factory=list)      # {at, query, duration}
    sfx: list[dict] = field(default_factory=list)           # {at, query, when}
    chapters: list[dict] = field(default_factory=list)      # {at, title}
    notes: list[str] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Plan":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


SCHEMA = {
    "type": "object",
    "properties": {
        "drop": {
            "type": "array",
            "description": "Blocks to cut from the video.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "reason": {"type": "string", "description": "2-6 words, e.g. 'dead air chatter', 'repeated take', 'mic check'"},
            }, "required": ["id", "reason"]},
        },
        "extend": {
            "type": "array",
            "description": "Wordless gameplay worth keeping after a block (a fight, a fall, a reveal). Only when the transcript implies something is happening on screen.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "seconds": {"type": "number", "minimum": 1, "maximum": 60},
            }, "required": ["id", "seconds"]},
        },
        "broll": {
            "type": "array",
            "description": "Place one of the provided B-roll files over the gameplay while the commentary continues.",
            "items": {"type": "object", "properties": {
                "file": {"type": "string", "description": "Exact filename from the B-roll list."},
                "at": {"type": "string", "description": "Block id where it starts."},
                "duration": {"type": "number"},
                "reason": {"type": "string"},
            }, "required": ["file", "at", "duration"]},
        },
        "overlays": {
            "type": "array",
            "description": "Short pop-up graphics from the editing pack, e.g. 'diamond pickaxe item spin', 'creeper', 'arrow'.",
            "items": {"type": "object", "properties": {
                "at": {"type": "string"},
                "query": {"type": "string"},
                "duration": {"type": "number", "minimum": 0.5, "maximum": 6},
            }, "required": ["at", "query"]},
        },
        "sfx": {
            "type": "array",
            "description": "Sound effects from the editing pack, e.g. 'level up', 'villager', 'xp orb', 'whoosh', 'glass break'.",
            "items": {"type": "object", "properties": {
                "at": {"type": "string"},
                "query": {"type": "string"},
                "when": {"type": "string", "enum": ["start", "end"]},
            }, "required": ["at", "query"]},
        },
        "chapters": {
            "type": "array",
            "description": "YouTube chapters. The first one starts at the first kept block.",
            "items": {"type": "object", "properties": {
                "at": {"type": "string"},
                "title": {"type": "string", "description": "Under 40 characters."},
            }, "required": ["at", "title"]},
        },
        "notes": {
            "type": "array", "items": {"type": "string"},
            "description": "Anything the creator should check during review (max 5).",
        },
    },
    "required": ["drop", "chapters"],
}


def system_prompt(cfg: dict, video: dict) -> str:
    ch = cfg["channel"]
    lines = [
        f"You are the video editor for {ch.get('name') or 'this channel'}, a Minecraft YouTube channel. "
        "You receive a timestamped transcript of raw recording sessions, split into numbered "
        "blocks, and you produce an edit decision list.",
        "",
        "How to edit:",
        "- Cut: mic checks, 'is this recording', false starts and repeated takes (keep the best "
        "one, usually the last), long rambles that go nowhere, dead-air filler ('uhh, ok, so'), "
        "inventory shuffling and walking chatter with no payoff, setup talk before the video "
        "properly starts and outro talk after the goodbye.",
        "- Keep: the story of the session, reactions, jokes and personality, explanations of "
        "builds and plans, anything that sets up a later payoff. When in doubt, keep. A "
        "let's-play should feel natural, not choppy.",
        f"- Aim to keep roughly {cfg['edit'].get('target_keep', '60-80%')} of the talking.",
        "- Blocks marked [FORCED CUT] are already cut by the creator's spoken command; do not "
        "list them. Blocks marked [HIGHLIGHT] are moments the creator flagged as great: never "
        "cut them, and consider an extend, SFX or overlay there.",
        "- Blocks marked [CHAPTER] are where the creator wants a new chapter.",
        "- B-roll: place provided B-roll files where the commentary talks about what they "
        "show (e.g. a build timelapse over 'so I finished the walls'). Each file at most once.",
        "- Overlays and SFX: be tasteful, roughly one every 1-2 minutes, at real moments "
        "(found diamonds, died, a mob appeared, a joke landed). Use plain descriptive queries "
        "made of words from the vocabulary lists.",
        "- Chapters: 3-12 chapters for long videos, titled in the creator's voice.",
        "- Only use block ids that appear in the transcript.",
        "- Lines starting with [on screen ...] describe what was happening during a stretch "
        "where the creator wasn't talking (action 0 = nothing, 3 = intense). For action 2-3, "
        "keep that gameplay with an extend on the block just before it (up to 60s). Action 0-1 "
        "stretches are safe to cut. Use these notes for overlays and SFX too.",
        "",
        "Keep viewers watching (YouTube retention):",
        "- The first 30 seconds decide whether people stay. Get to the point fast: keep a quick "
        "greeting, but cut long 'hey guys, welcome back, hope you're all doing well' warm-ups, "
        "recaps nobody needs, and housekeeping (settings, mods, sponsors) before the action.",
        "- Within the first minute the viewer should know what this video is about and why it's "
        "worth watching (the goal, the challenge, the stakes). Keep the line where that is said.",
        "- Be tighter in the first 3-5 minutes than later on; that's where most viewers leave.",
        "- Every few minutes something should change: a new goal, a discovery, danger, a joke, "
        "a reveal. Cut stretches where nothing changes and nothing is set up.",
        "- Keep set-ups that pay off later, and always keep the payoff and the reaction to it.",
        "- After the climax or the goodbye, end quickly. Cut long outros; keep one short sign-off.",
        "- Never cut the creator's personality to save time. Jokes and reactions are why people "
        "subscribe to a let's-play.",
    ]
    if ch.get("style_notes"):
        lines += ["", "Creator's own style notes:", ch["style_notes"]]
    if video.get("topic"):
        lines += ["", f"This video: {video.get('topic')}"
                  + (f" (series: {video['series']}" + (f", episode {video['episode']}" if video.get('episode') else "") + ")"
                     if video.get("series") else "")]
    return "\n".join(lines)


def format_blocks(blocks: list[Block], recordings: list[Path], context: list[Block] = (),
                  notes: dict[str, list[str]] | None = None) -> str:
    out = []
    if context:
        out.append("(Earlier blocks, for context only. Do not edit these.)")
        for b in context:
            out.append(f"  {b.id} {b.text}")
        out.append("")
    last_src = None
    for b in blocks:
        if b.source != last_src:
            out.append(f"--- Recording {b.source + 1}: {recordings[b.source].name} ---")
            last_src = b.source
        tags = []
        if "forced_drop" in b.flags:
            tags.append("[FORCED CUT]")
        if "highlight" in b.flags:
            tags.append("[HIGHLIGHT]")
        if "chapter" in b.flags:
            tags.append("[CHAPTER]")
        tag_str = " " + " ".join(tags) if tags else ""
        out.append(f"{b.id} [{fmt_ts(b.start)} {b.duration:.0f}s]{tag_str} {b.text}")
        for note in (notes or {}).get(b.id, []):
            out.append(f"      {note}")
    return "\n".join(out)


def user_prompt(blocks_text: str, broll: list[tuple[str, float]], vocab: dict[str, list[str]]) -> str:
    parts = []
    if broll:
        parts.append("B-roll files available (filename, length):")
        parts += [f"- {name} ({dur:.0f}s)" for name, dur in broll]
    else:
        parts.append("No B-roll files were provided for this video.")
    for cat, words in vocab.items():
        if words:
            parts.append(f"\nWords that appear in the {cat} library: {', '.join(words)}")
    parts.append("\nTranscript:\n" + blocks_text)
    parts.append("\nReturn the edit by calling the tool.")
    return "\n".join(parts)


def plan_without_ai(blocks: list[Block]) -> Plan:
    """Fallback: honour spoken cues and trim silences only."""
    plan = Plan()
    first = next((b for b in blocks if "forced_drop" not in b.flags), None)
    if first:
        plan.chapters.append({"at": first.id, "title": "Intro"})
    n = 2
    for b in blocks:
        if "chapter" in b.flags and "forced_drop" not in b.flags and b is not first:
            plan.chapters.append({"at": b.id, "title": f"Part {n}"})
            n += 1
    return plan


ToolCaller = Callable[..., dict]


def make_plan(blocks: list[Block], recordings: list[Path], cfg: dict, video: dict,
              broll: list[tuple[str, float]], vocab: dict[str, list[str]],
              caller: ToolCaller = llm.call_tool, log=print,
              notes: dict[str, list[str]] | None = None) -> Plan:
    window = max(int(cfg["llm"].get("window_blocks", 600)), 50)
    valid = {b.id for b in blocks}
    by_id = {b.id: b for b in blocks}
    plan = Plan()
    sys_prompt = system_prompt(cfg, video)
    windows = [blocks[i:i + window] for i in range(0, len(blocks), window)] or [[]]
    used_broll: set[str] = set()

    for w_idx, chunk in enumerate(windows):
        if not chunk:
            continue
        start = blocks.index(chunk[0])
        context = blocks[max(0, start - 15):start]
        log(f"AI edit pass {w_idx + 1}/{len(windows)} ({len(chunk)} blocks)")
        remaining_broll = [(n, d) for n, d in broll if n not in used_broll]
        result = caller(
            cfg, sys_prompt,
            user_prompt(format_blocks(chunk, recordings, context, notes), remaining_broll, vocab),
            "submit_edit", "Submit the edit decision list for these transcript blocks.", SCHEMA,
        )
        chunk_ids = {b.id for b in chunk}

        for d in result.get("drop", []):
            bid = d.get("id")
            if bid in chunk_ids and "highlight" not in by_id[bid].flags:
                plan.drop[bid] = d.get("reason", "")
        for e in result.get("extend", []):
            if e.get("id") in chunk_ids:
                plan.extend[e["id"]] = float(min(max(e.get("seconds", 0), 0), 60))
        broll_names = {n for n, _ in broll}
        for item in result.get("broll", []):
            if item.get("at") in chunk_ids and item.get("file") in broll_names and item["file"] not in used_broll:
                used_broll.add(item["file"])
                plan.broll.append(item)
        for key in ("overlays", "sfx", "chapters"):
            for item in result.get(key, []):
                if item.get("at") in valid:
                    getattr(plan, key).append(item)
        plan.notes += [n for n in result.get("notes", []) if isinstance(n, str)][:5]

    # Spoken chapter cues always win a chapter, even if the AI forgot them.
    chaptered = {c["at"] for c in plan.chapters}
    for b in blocks:
        if "chapter" in b.flags and b.id not in chaptered:
            plan.chapters.append({"at": b.id, "title": "New chapter"})
    plan.chapters.sort(key=lambda c: c["at"])
    return plan
