"""The AI editor: reads the transcript and decides what to cut, keep and decorate."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from kdcut import llm
from kdcut.blocks import Block, fmt_ts


@dataclass
class Plan:
    drop: dict[str, str] = field(default_factory=dict)      # block id -> reason
    extend: dict[str, float] = field(default_factory=dict)  # block id -> seconds of silent gameplay to keep after it
    fast: list[dict] = field(default_factory=list)          # {id, seconds, speed, reason}: sped-up quiet stretch after a block
    montage: list[dict] = field(default_factory=list)       # {id, seconds, reason}: quiet stretch cut to the beat of a song
    broll: list[dict] = field(default_factory=list)         # {file, at, start_words, end_words, clip_from, clip_to, reason}
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


RULES_VERSION = 9     # bump when the editing rules change, so saved plans are redone


def blocks_key(blocks: list[Block]) -> str:
    import hashlib
    sig = f"v{RULES_VERSION}|" + "|".join(f"{b.id}:{b.source}:{b.start:.2f}:{b.end:.2f}" for b in blocks)
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]


def _words(text: str) -> list[str]:
    import re
    return re.findall(r"[a-z0-9']+", text.lower())


def is_restart(cut_text: str, next_text: str, near: int = 8) -> bool:
    """True when `next_text` starts over what `cut_text` said (a retake): "right i got
    myself a couple pieces of food" followed by "all right i got myself a couple pieces of
    food now...". Most of the cut words come back, in order, near the start of the next block."""
    import difflib
    cut = _words(cut_text)[-25:]
    nxt = _words(next_text)
    if len(cut) < 3 or not nxt:
        return False
    sm = difflib.SequenceMatcher(None, cut, nxt[:len(cut) + near], autojunk=False)
    hits = [m for m in sm.get_matching_blocks() if m.size]
    if not hits or hits[0].b > near:
        return False
    return sum(m.size for m in hits) >= max(3, 0.6 * len(cut))


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
            if (bad_start or bad_end) and nxt is not None and is_restart(
                    " ".join(b.text for b in src_blocks[i:j + 1]), nxt.text):
                bad_start = bad_end = False     # a false start: the next block says it again
            if bad_start or bad_end:
                for b in src_blocks[i:j + 1]:
                    plan.drop.pop(b.id, None)
                    restored.append(b.id)
            i = j + 1
    # A sentence carried across clips ("And as for bookshelves." / "They are also" / "a check."):
    # keep every part if any part stays.
    changed = True
    while changed:
        changed = False
        for a, b in zip(blocks, blocks[1:]):
            if a.source == b.source or not continues_across(a, b):
                continue
            if any("forced_drop" in x.flags or x.cue for x in (a, b)):
                continue
            if dropped(a) != dropped(b):
                gone = a if dropped(a) else b
                plan.drop.pop(gone.id, None)
                restored.append(gone.id)
                changed = True
    if restored:
        plan.notes.append(f"Kept {len(restored)} block(s) the AI wanted to cut, because cutting "
                          "them would have split a sentence: " + ", ".join(restored))
    return restored


def protect_joins(plan: Plan, blocks: list[Block], voices: dict, max_seconds: float = 2.5,
                  near: float = 1.0) -> list[str]:
    """Keep short cut fragments that can't be cut cleanly. When the creator says two things
    in one breath ("oh baby, that new world feeling, let's go"), there's no silence between
    them, so cutting the second part leaves a chopped word. Two extra words are far better
    than a glitchy cut. Retakes ("right I got..." / "all right I got...") still go: there the
    repeat matters more than a slightly tight cut."""
    restored = []
    by_src: dict[int, list[Block]] = {}
    for b in blocks:
        by_src.setdefault(b.source, []).append(b)
    for src, bl in by_src.items():
        v = voices.get(src)
        if v is None:
            continue
        i = 0
        while i < len(bl):
            if bl[i].id not in plan.drop or "forced_drop" in bl[i].flags:
                i += 1
                continue
            j = i
            while j + 1 < len(bl) and bl[j + 1].id in plan.drop and "forced_drop" not in bl[j + 1].flags:
                j += 1
            run, prev = bl[i:j + 1], (bl[i - 1] if i > 0 else None)
            nxt = bl[j + 1] if j + 1 < len(bl) else None
            talk = sum(b.duration for b in run)
            text = " ".join(b.text for b in run)
            glued = False
            if prev is not None and prev.id not in plan.drop and run[0].start - prev.end < near:
                glued |= not v.cut_between(prev.end, run[0].start)[1]
            if nxt is not None and nxt.id not in plan.drop and nxt.start - run[-1].end < near:
                glued |= not v.cut_between(run[-1].end, nxt.start)[1]
            if glued and talk <= max_seconds and not (nxt is not None and is_restart(text, nxt.text)):
                for b in run:
                    plan.drop.pop(b.id, None)
                    restored.append(b.id)
            i = j + 1
    if restored:
        plan.notes.append(f"Kept {len(restored)} short bit(s) the AI wanted to cut, because they were said "
                          "in the same breath as what's kept and couldn't be cut cleanly: " + ", ".join(restored))
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
        "fast": {
            "type": "array",
            "description": "Rarely: speed up a quiet stretch after a block when seeing the trip or the "
                           "wait is part of the story. A few per video at most; most quiet stretches "
                           "are cut.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "The block the quiet stretch follows."},
                "seconds": {"type": "number", "minimum": 4, "maximum": 900,
                            "description": "How much of the quiet stretch to show (real seconds)."},
                "speed": {"type": "number", "minimum": 2, "maximum": 12},
                "reason": {"type": "string", "description": "2-6 words, e.g. 'walk back to base'"},
            }, "required": ["id", "seconds", "speed"]},
        },
        "montage": {
            "type": "array",
            "description": "Hands-on quiet stretches (building, mining, digging, clearing land, "
                           "farming) cut into a beat-synced montage, block sounds on the beat. "
                           "Use this, not fast, for hands-on work.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "The block the quiet stretch follows."},
                "seconds": {"type": "number", "minimum": 20, "maximum": 900,
                            "description": "How much of the quiet stretch to use (real seconds)."},
                "reason": {"type": "string", "description": "2-6 words, e.g. 'building the walls'"},
            }, "required": ["id", "seconds"]},
        },
        "broll": {
            "type": "array",
            "description": "Show part of a provided B-roll file over the gameplay while the commentary continues.",
            "items": {"type": "object", "properties": {
                "file": {"type": "string", "description": "Exact filename from the B-roll list."},
                "at": {"type": "string", "description": "Block id where the creator mentions what it shows."},
                "start_words": {"type": "string",
                                "description": "The exact 3-6 words (copied from the transcript) where the "
                                               "B-roll should appear: where the thing it shows is named, e.g. "
                                               "'got some kelp'. May run into the next block."},
                "end_words": {"type": "string",
                              "description": "Optional: the exact 2-6 words where talk about it ends."},
                "clip_from": {"type": "number", "description": "Seconds into the file to start from (0 = the start)."},
                "clip_to": {"type": "number", "description": "Seconds into the file to stop. Use only the part "
                                                             "that matches what's being said, usually 4-12s."},
                "reason": {"type": "string"},
            }, "required": ["file", "at", "start_words", "clip_from", "clip_to"]},
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


OVERLAY_RULE = (
    "- Overlays (item spins, mob pictures) are for list moments only: when the creator "
    "announces the goal or plan ('today I'm building an iron farm' -> iron ingot with the "
    "text 'Iron Farm'), or names things to come or to watch out for ('we'll have to worry "
    "about blazes and spiders' -> blaze, then spider, a second or so each). Never pop one "
    "up just because the creator is looking at or mining something. Few videos need more "
    "than 2-4. Give a short on-screen text label when it names the goal or topic."
)


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
        "- The creator records short clips on purpose and narrates them for the edit. Every clip "
        "was recorded because something in it matters; recording headers say how long each clip "
        "is. Respect his hard clues: checklists ('gear check, nether check'), a sentence spread "
        "over several clips ('And as for bookshelves.' / 'They are also' / 'a check.') where the "
        "footage in between shows the thing, 'let's go' at the end of a plan, 'there's footage on "
        "screen now'. A short line at a clip boundary is usually part of such a sentence, not a "
        "stray fragment: never cut part of one. Lines marked '↪' continue in the next clip.",
        "- [BUILD CUE] lines ('let's build this thing', 'cue the music', 'start a montage of "
        "mining diamonds', 'let's queue a time lapse') say the work starts now. The quiet "
        "stretch after one, and any NO TALKING clips right after it, is that work: show it, don't "
        "drop it. Long hands-on work is a montage; shorter work keeps a few seconds in real time "
        "with an extend. Clips with NO TALKING were recorded on purpose for exactly this.",
        "- Every build needs its payoff: keep the line that says it's done, the test ('let's "
        "see if this works') and the result (the loot, 'it works'). Keep a few seconds of the "
        "finished build with an extend on the 'it's done' line.",
        "- [TIME SKIP] lines ('it's nighttime', 'okay, it's been two minutes') skip a wait: the "
        "wait before them is cut with a hard cut. No fast, montage or extend on it.",
        "- Running gags and callbacks ('as is tradition, let's check the wheat farm') are a "
        "thread through the episode: keep every time it comes back.",
        "- Read the [on screen] notes before cutting a short line: 'Well, that's one way to get "
        "back' over a death screen is the story (he died), not a fragment.",
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
        "cut them, and consider an extend or SFX there.",
        "- Blocks marked [CHAPTER] are where the creator wants a new chapter.",
        "- B-roll: show a provided B-roll file at the moment the creator names what it shows, "
        "not where the topic starts: set start_words to the words that name it (e.g. 'got some "
        "kelp', 'wandering trader come and visit me'). Each file's list shows what is on screen "
        "when; use just the matching part (clip_from/clip_to), usually 4-12 seconds, not the "
        "whole file. Match on what the file shows, not just its name. Spread them out: never two "
        "on the same sentence. A file can be used again for a different part of it. When the "
        "creator says footage is on screen ('there's footage on screen now'), the B-roll for "
        "that goes right there. B-roll is optional: skip any file that doesn't clearly match "
        "something the creator says; using none is fine.",
        OVERLAY_RULE if cfg["edit"].get("overlays", False) else
        "- Don't add on-screen graphics or text; the creator adds those by hand.",
        "- Sound effects: only where a viewer would expect a sound: a punchline, a fail, a "
        "jump scare, a big reveal. Never ambience or background sounds. Fewer is better; none is "
        "fine. Use plain descriptive queries made of words from the vocabulary lists.",
        "- Chapters: 3-12 chapters for long videos, titled in the creator's voice.",
        "- Only use block ids that appear in the transcript.",
        "- The STORY OUTLINE was written from the whole episode before editing. Follow it: beats "
        "marked 'matters 3' are the spine of the episode, so keep them (trim only waste inside "
        "them). 'matters 1' beats are the first candidates to go when they're also dull. Keep "
        "both ends of every thread: a setup whose payoff stays must stay.",
        "- Blocks carry measurements from the footage in braces, scored 1-5 against the rest of "
        "this episode: energy (how animated the creator's voice is), action (how much the picture "
        "changes), menus (share of time in inventory/chest/crafting screens) and hits (sudden loud "
        "game sounds). Lines like [quiet 45s after: action 1 · menus 80%] describe the wordless "
        "stretch after a block. Treat them as evidence, never as the decision: the story comes "
        "first, and a calm block that the story needs stays even at energy 1.",
        "- Use the numbers where the story doesn't decide: low energy + low action talk that "
        "doesn't move the story is the first to cut; menu-heavy stretches (sorting chests, "
        "crafting) are usually cuttable unless the creator is explaining something that matters.",
        "- Quiet stretches: keep the lively ones in real time with an extend (action 4-5, hits, "
        "anything the outline calls danger or discovery, up to 60s). Everything else that's "
        "quiet is simply cut: a jump cut is normal and keeps the video moving.",
        f"- Fast-forward is seasoning, not the default: at most {max_fast(cfg)} in the whole video. "
        "Use one only where seeing the trip or the wait is part of the story (the long search "
        "he talks about, the journey he complains about), at speed 3-8 for 3-15 s on screen. "
        "Ordinary walking, flying, exploring and corridors are cut, not sped up. Never two "
        "fast-forwards close together, never right next to a montage, never over talking.",
        "- An extend is for showing something (a fight, a reveal, the thing he just named, like "
        "the bookshelf he's checking off). Never extend just to hold a view while he pauses "
        "before talking again: dead air is always cut.",
        f"- Montage: a long quiet stretch of hands-on work (building, mining, digging, clearing "
        f"land, farming; at least {montage_min_footage(cfg):.0f}s of footage) is cut into a montage "
        "to the beat of a song, with the block sounds on the beat. It's a real section of the "
        f"video, at least 20 s on screen, using footage from the whole stretch. Up to {max_montages(cfg)} "
        "per video, the best stretches first. A "
        "shorter bit of work is never a montage: keep a few seconds with an extend or cut it. "
        "Never where the creator talks, never travel.",
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


def continues_across(a: Block, b: Block) -> bool:
    """Does the last line of one clip carry on in the first line of the next? ('They are
    also' / 'a check.', 'And as for bookshelves.' / 'They are also')"""
    from kdcut.blocks import ends_sentence
    words_a = a.text.strip().split()
    if not words_a:
        return False
    if not ends_sentence(a.text):
        return True
    # Whisper puts a full stop on every clip's last line. A short line without a verb that
    # sets up a topic ("And as for bookshelves.") is still unfinished.
    return len(words_a) <= 5 and words_a[0].lower().strip(",") in {"and", "as", "so", "but", "then", "plus"} \
        and b.duration < 6


def format_blocks(blocks: list[Block], recordings: list[Path], context: list[Block] = (),
                  notes: dict[str, list[str]] | None = None, measured: dict[str, str] | None = None,
                  beats: dict[str, dict] | None = None, durations: list[float] | None = None) -> str:
    out = []
    if context:
        out.append("(Earlier blocks, for context only. Do not edit these.)")
        for b in context:
            out.append(f"  {b.id} {b.text}")
        out.append("")
    from kdcut.blocks import ends_sentence
    last_src, last_beat = None, None
    for k, b in enumerate(blocks):
        if b.source != last_src:
            if last_src is not None:
                # Clips with no talking at all between the last one and this one.
                for q in range(last_src + 1, b.source):
                    length = f", {durations[q]:.0f}s" if durations and q < len(durations) else ""
                    out.append(f"--- Recording {q + 1}: {recordings[q].name}{length} clip, NO TALKING ---")
            length = (f", {durations[b.source]:.0f}s clip" if durations and b.source < len(durations) else "")
            out.append(f"--- Recording {b.source + 1}: {recordings[b.source].name}{length} ---")
            last_src = b.source
        beat = (beats or {}).get(b.id)
        if beat is not None and beat is not last_beat:
            out.append(f"  == {beat.get('kind')} (matters {beat.get('matters')}): {beat.get('what')}")
        last_beat = beat
        tags: list[str] = []
        if "forced_drop" in b.flags:
            tags.append("[FORCED CUT]")
        if "highlight" in b.flags:
            tags.append("[HIGHLIGHT]")
        if "chapter" in b.flags:
            tags.append("[CHAPTER]")
        if "build_cue" in b.flags:
            tags.append("[BUILD CUE]")
        if "time_skip" in b.flags:
            tags.append("[TIME SKIP]")
        tag_str = " " + " ".join(tags) if tags else ""
        if (measured or {}).get(b.id):
            tag_str += " " + measured[b.id]
        nxt = blocks[k + 1] if k + 1 < len(blocks) else None
        runs_on = (nxt is not None and nxt.source == b.source and not ends_sentence(b.text)
                   and nxt.start - b.end < 1.2)
        across = (nxt is not None and nxt.source != b.source and continues_across(b, nxt))
        out.append(f"{b.id} [{fmt_ts(b.start)} {b.duration:.0f}s]{tag_str} {b.text}"
                   + (" →" if runs_on else "") + (" ↪" if across else ""))
        for note in (notes or {}).get(b.id, []):
            out.append(f"      {note}")
    return "\n".join(out)


def user_prompt(blocks_text: str, broll: list[tuple], vocab: dict[str, list[str]],
                outline_text: str = "") -> str:
    parts = []
    if outline_text:
        parts.append("STORY OUTLINE (whole episode):\n" + outline_text + "\n")
    if broll:
        parts.append("B-roll files available (filename, length: what's on screen when):")
        for name, dur, *shows in broll:
            what = shows[0] if shows and shows[0] else ""
            parts.append(f"- {name} ({dur:.0f}s)" + (f": {what}" if what else ""))
    else:
        parts.append("No B-roll files were provided for this video.")
    for cat, words in vocab.items():
        if words:
            parts.append(f"\nWords that appear in the {cat} library: {', '.join(words)}")
    parts.append("\nTranscript:\n" + blocks_text)
    parts.append("\nReturn the edit by calling the tool.")
    return "\n".join(parts)


def max_montages(cfg: dict) -> int:
    return max(int(cfg.get("edit", {}).get("max_montages", 4)), 0)


def max_fast(cfg: dict) -> int:
    return max(int(cfg.get("edit", {}).get("max_fast_forwards", 3)), 0)


def montage_min_footage(cfg: dict) -> float:
    return float(cfg.get("edit", {}).get("montage_min_footage", 30))


HANDS_ON = re.compile(r"\b(build|building|built|mine|mining|strip|dig|digging|tunnel\w*|quarry\w*|clear\w*|"
                      r"terraform\w*|farm|farming|harvest\w*|chop\w*|log(ging)?|plac\w+|break\w*|wall\w*|"
                      r"roof\w*|decorat\w*|landscap\w*|path\w*|bridge|fill\w*|flatten\w*)\b", re.I)
TRAVEL = re.compile(r"\b(walk\w*|ride|riding|rode|fly\w*|flight|elytra|sail\w*|boat|swim\w*|run|running|"
                    r"travel\w*|journey|trek|head\w*|back home|explor\w*|look\w* for|search\w*|hunt\w*|"
                    r"corridor\w*|portal|wait\w*|night)\b", re.I)


def promote_montages(plan: Plan, cfg: dict) -> list[str]:
    """Hands-on stretches the AI made fast-forwards become montages, up to the limit: the
    channel prefers a montage for building and mining. Longest first. Returns notes."""
    room = max_montages(cfg) - len(plan.montage)
    if room <= 0 or not cfg.get("edit", {}).get("montages", True):
        return []
    taken = {m["id"] for m in plan.montage}
    picks = [f for f in plan.fast if f["id"] not in taken
             and float(f.get("seconds", 0)) >= montage_min_footage(cfg)
             and HANDS_ON.search(f.get("reason") or "") and not TRAVEL.search(f.get("reason") or "")]
    picks = sorted(picks, key=lambda f: -float(f.get("seconds", 0)))[:room]
    for f in picks:
        plan.fast.remove(f)
        plan.montage.append({"id": f["id"], "seconds": float(f["seconds"]), "reason": f.get("reason") or ""})
    return [f"Made {f['id']} ({f.get('reason')}) a montage instead of a fast-forward" for f in picks]


def space_out(plan: Plan, blocks: list[Block], cfg: dict) -> list[str]:
    """Keep speed effects from piling up: montages shorter than the minimum footage go, then
    fast-forwards are capped and spaced out. Montages win (they're the feature), then the
    longest fast-forwards, the ones most worth showing. What's left over is simply cut.
    Returns notes for the log."""
    ed = cfg.get("edit", {})
    order = {b.id: i for i, b in enumerate(blocks)}
    gap = int(ed.get("fast_spacing_blocks", 8))
    notes = []
    short = [m for m in plan.montage if float(m.get("seconds", 0)) < montage_min_footage(cfg)]
    for m in short:
        plan.montage.remove(m)
        notes.append(f"No montage after {m['id']} ({m.get('reason')}): only {float(m['seconds']):.0f}s of "
                     "footage, too little for a real montage")
    anchors = [order[m["id"]] for m in plan.montage if m["id"] in order]
    keep = []
    for f in sorted(plan.fast, key=lambda f: -float(f.get("seconds", 0))):
        i = order.get(f["id"])
        if i is None:
            continue
        why = ""
        if len(keep) >= max_fast(cfg):
            why = f"already {max_fast(cfg)} fast-forwards"
        elif any(abs(i - a) < gap for a in anchors):
            why = "too close to another fast-forward or montage"
        if why:
            notes.append(f"Cut the quiet stretch after {f['id']} ({f.get('reason')}) instead of fast-forwarding: {why}")
            continue
        keep.append(f)
        anchors.append(i)
    plan.fast = sorted(keep, key=lambda f: order.get(f["id"], 0))
    return notes


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

FAST_MAX_ON_SCREEN = 20.0


def clean_fast(f: dict, ids: set[str], quiet_seconds: dict[str, float]) -> dict | None:
    """A sane speed-up: real seconds within the quiet stretch, 2-12x, at most 20 s on screen."""
    bid = f.get("id")
    if bid not in ids:
        return None
    try:
        secs, speed = float(f.get("seconds", 0)), float(f.get("speed", 4))
    except (TypeError, ValueError):
        return None
    if bid in quiet_seconds:
        secs = min(secs, quiet_seconds[bid])
    speed = min(max(speed, 2.0), 12.0)
    secs = min(secs, FAST_MAX_ON_SCREEN * speed)
    if secs < 4.0:
        return None
    return {"id": bid, "seconds": round(secs, 1), "speed": round(speed, 2), "reason": str(f.get("reason", ""))}


def make_plan(blocks: list[Block], recordings: list[Path], cfg: dict, video: dict,
              broll: list[tuple], vocab: dict[str, list[str]],
              caller: ToolCaller = llm.call_tool, log=print,
              notes: dict[str, list[str]] | None = None, measured: dict[str, str] | None = None,
              outline=None, quiet_seconds: dict[str, float] | None = None,
              durations: list[float] | None = None) -> Plan:
    window = max(int(cfg["llm"].get("window_blocks", 600)), 50)
    valid = {b.id for b in blocks}
    by_id = {b.id: b for b in blocks}
    plan = Plan()
    sys_prompt = system_prompt(cfg, video)
    windows = [blocks[i:i + window] for i in range(0, len(blocks), window)] or [[]]
    used_broll: dict[str, list[tuple[float, float]]] = {}     # file -> parts already shown
    beats = None
    outline_text = ""
    if outline is not None:
        from kdcut import story
        beats = story.beat_of(outline, blocks)
        outline_text = outline.text()

    for w_idx, chunk in enumerate(windows):
        if not chunk:
            continue
        start = blocks.index(chunk[0])
        context = blocks[max(0, start - 15):start]
        log(f"AI edit pass {w_idx + 1}/{len(windows)} ({len(chunk)} blocks)")
        remaining_broll = list(broll)
        result = caller(
            cfg, sys_prompt,
            user_prompt(format_blocks(chunk, recordings, context, notes, measured, beats, durations), remaining_broll,
                        vocab, outline_text),
            "submit_edit", "Submit the edit decision list for these transcript blocks.",
            SCHEMA if cfg["edit"].get("overlays", False) else
            {**SCHEMA, "properties": {k: v for k, v in SCHEMA["properties"].items() if k != "overlays"}},
        )
        chunk_ids = {b.id for b in chunk}

        for d in result.get("drop", []):
            bid = d.get("id")
            if bid in chunk_ids and "highlight" not in by_id[bid].flags:
                plan.drop[bid] = d.get("reason", "")
        for e in result.get("extend", []):
            if e.get("id") in chunk_ids:
                plan.extend[e["id"]] = float(min(max(e.get("seconds", 0), 0), 60))
        for m in result.get("montage", []):
            item = clean_fast({**m, "speed": 4}, chunk_ids, (quiet_seconds or {}))
            if item and len(plan.montage) < max_montages(cfg) and all(x["id"] != item["id"] for x in plan.montage):
                plan.montage.append({"id": item["id"], "seconds": float(min(float(m.get("seconds", 0)),
                                                                            (quiet_seconds or {}).get(item["id"], 1e9))),
                                     "reason": item["reason"]})
        for f in result.get("fast", []):
            item = clean_fast(f, chunk_ids, (quiet_seconds or {}))
            if item and all(x["id"] != item["id"] for x in plan.fast):
                plan.fast.append(item)
        broll_names = {b[0] for b in broll}
        for item in result.get("broll", []):
            if item.get("at") not in chunk_ids or item.get("file") not in broll_names:
                continue
            try:
                part = (float(item.get("clip_from") or 0), float(item.get("clip_to") or 0))
            except (TypeError, ValueError):
                part = (0.0, 0.0)
            shown = used_broll.setdefault(item["file"], [])
            # The same part of a file twice would look like a mistake.
            if any(part[0] < b and a < (part[1] if part[1] > part[0] else 1e9) for a, b in shown) \
                    or (shown and part[1] <= part[0]):
                continue
            shown.append((part[0], part[1] if part[1] > part[0] else 1e9))
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
    if outline is not None:
        from kdcut import story
        kept_setups = story.protect_threads(plan, outline, blocks)
        if kept_setups:
            log(f"Kept {len(kept_setups)} set-up block(s) the story needs")
    restored = protect_sentences(plan, blocks)
    if restored:
        log(f"Kept {len(restored)} block(s) whose cut would have split a sentence")
    plan.blocks_key = blocks_key(blocks)
    return plan
