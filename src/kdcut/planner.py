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
    blocks_key: str = ""     # which transcript blocks this plan was made for

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Plan":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def blocks_key(blocks: list[Block]) -> str:
    import hashlib
    sig = "|".join(f"{b.id}:{b.source}:{b.start:.2f}:{b.end:.2f}" for b in blocks)
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]


def protect_sentences(plan: Plan, blocks: list[Block], joined: float = 1.2) -> list[str]:
    """Undo cuts that would split a sentence: a run of dropped blocks may only start right
    after a finished sentence (or a real pause) and end on one. Returns what was restored."""
    from kdcut.blocks import ends_sentence
    restored = []
    by_src: dict[int, list[Block]] = {}
    for b in blocks:
        by_src.setdefault(b.source, []).append(b)

    def dropped(b: Block) -> bool:
        return b.id in plan.drop

    for src_blocks in by_src.values():
        i = 0
        while i < len(src_blocks):
            if not dropped(src_blocks[i]):
                i += 1
                continue
            j = i
            while j + 1 < len(src_blocks) and dropped(src_blocks[j + 1]):
                j += 1
            first, last = src_blocks[i], src_blocks[j]
            prev = src_blocks[i - 1] if i > 0 else None
            nxt = src_blocks[j + 1] if j + 1 < len(src_blocks) else None
            bad_start = (prev is not None and "forced_drop" not in prev.flags and not prev.cue
                         and not ends_sentence(prev.text) and first.start - prev.end < joined)
            bad_end = (nxt is not None and "forced_drop" not in nxt.flags and not nxt.cue
                       and not ends_sentence(last.text) and nxt.start - last.end < joined)
            if bad_start or bad_end:
                for b in src_blocks[i:j + 1]:
                    plan.drop.pop(b.id, None)
                    restored.append(b.id)
            i = j + 1
    if restored:
        plan.notes.append(f"Kept {len(restored)} block(s) the AI wanted to cut, because cutting "
                          "them would have split a sentence: " + ", ".join(restored))
    return restored


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
            "description": "Pop-up graphics from the editing pack for list moments (the goal, things to come), "
                           "e.g. 'iron ingot item spin', 'blaze', 'spider'.",
            "items": {"type": "object", "properties": {
                "at": {"type": "string"},
                "query": {"type": "string"},
                "duration": {"type": "number", "minimum": 0.5, "maximum": 6},
                "text": {"type": "string", "description": "Optional on-screen label, 1-3 words, e.g. 'Iron Farm'."},
            }, "required": ["at", "query"]},
        },
        "sfx": {
            "type": "array",
            "description": "Sound effects for clear moments only (punchline, fail, scare, reveal), e.g. 'level up', 'xp orb', 'whoosh', 'glass break'. Never ambience.",
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
        "- This is a story-driven let's-play: the creator talking IS the content. The viewer "
        "must be able to follow the whole story: why they're going "
        "somewhere, what they're looking for, what they found. Keep those lines even when "
        "they're plain.",
        "- Personal stories, life updates and backstory are personality, not filler. Keep them; "
        "at most trim a part that truly repeats itself.",
        "- Think like a storyteller: keep what is personal and what moves toward the goals the "
        "creator states (the plan, the attempts that matter, the payoff). Cut detours that go "
        "nowhere and don't change the story: e.g. 'let me check this shore for iron' -> finds "
        "nothing -> 'ok, heading across'. Cut the whole detour, from the line that starts it to "
        "the line that gives up, as long as nothing later refers back to it.",
        "- Also cut clear waste: mic checks, 'is this recording', false starts and repeated takes "
        "(keep the best one, usually the last), the same point said twice in a row, stray "
        "one-word fragments that lead nowhere, and talk after the goodbye.",
        "- Never cut in the middle of a sentence. Blocks ending with '→' run straight into the "
        "next block: cut both or neither. A cut must start after a finished sentence and end on "
        "one, so what's left reads naturally.",
        "- There is no target length. Don't cut to hit a percentage; when in doubt, keep. "
        "A let's-play should feel natural, not choppy.",
        "- Blocks marked [FORCED CUT] are already cut by the creator's spoken command; do not "
        "list them. Blocks marked [HIGHLIGHT] are moments the creator flagged as great: never "
        "cut them, and consider an extend, SFX or overlay there.",
        "- Blocks marked [CHAPTER] are where the creator wants a new chapter.",
        "- B-roll: place provided B-roll files where the commentary talks about what they "
        "show (e.g. a build timelapse over 'so I finished the walls'). Each file at most once.",
        "- Overlays (item spins, mob pictures) are for list moments only: when the creator "
        "announces the goal or plan ('today I'm building an iron farm' -> iron ingot with the "
        "text 'Iron Farm'), or names things to come or to watch out for ('we'll have to worry "
        "about blazes and spiders' -> blaze, then spider, a second or so each). Never pop one "
        "up just because the creator is looking at or mining something. Few videos need more "
        "than 2-4. Give a short on-screen text label when it names the goal or topic.",
        "- Sound effects: only where a viewer would expect a sound: a punchline, a fail, a "
        "jump scare, a big reveal. Never ambience or background sounds. Fewer is better; none is "
        "fine. Use plain descriptive queries made of words from the vocabulary lists.",
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
        "- Be a little tighter in the first few minutes, but only by cutting waste, never story "
        "or personality.",
        "- Every few minutes something should change: a new goal, a discovery, danger, a joke, "
        "a reveal. Long silent stretches where nothing happens on screen can go.",
        "- Keep set-ups that pay off later, and always keep the payoff and the reaction to it.",
        "- After the climax or the goodbye, end quickly. Cut long outros; keep one short sign-off.",
        "- Never cut the creator's personality to save time. Jokes, stories and reactions are why "
        "people subscribe to a let's-play.",
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
    from kdcut.blocks import ends_sentence
    last_src = None
    for k, b in enumerate(blocks):
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
        nxt = blocks[k + 1] if k + 1 < len(blocks) else None
        runs_on = (nxt is not None and nxt.source == b.source and not ends_sentence(b.text)
                   and nxt.start - b.end < 1.2)
        out.append(f"{b.id} [{fmt_ts(b.start)} {b.duration:.0f}s]{tag_str} {b.text}" + (" →" if runs_on else ""))
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
    restored = protect_sentences(plan, blocks)
    if restored:
        log(f"Kept {len(restored)} block(s) whose cut would have split a sentence")
    plan.blocks_key = blocks_key(blocks)
    return plan
