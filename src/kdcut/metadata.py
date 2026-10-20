"""Titles, description, tags, chapters and thumbnail ideas for the finished edit."""

from __future__ import annotations

from dataclasses import dataclass, field

from kdcut import llm
from kdcut.blocks import Block, fmt_ts

SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"},
                   "description": "5 title options, under 70 characters, curiosity-driven but honest."},
        "description": {"type": "string",
                        "description": "2-4 short paragraphs for the YouTube description. No chapter list (added separately), no hashtags."},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "10-15 search tags."},
        "thumbnail_text": {"type": "array", "items": {"type": "string"},
                           "description": "3 options of 1-4 words for big thumbnail text."},
        "thumbnail_moments": {"type": "array", "items": {"type": "string"},
                              "description": "3-5 block ids where the on-screen moment would make a strong thumbnail frame."},
        "cold_open": {
            "type": "object",
            "description": "Only when asked for. A 4-10 second teaser of the most exciting moment, "
                           "shown before the video starts to hook viewers. Omit if nothing is strong enough.",
            "properties": {
                "id": {"type": "string", "description": "Block id of the moment."},
                "start_words": {"type": "string",
                                "description": "The exact first 3-6 words of the teaser, copied from that block."},
                "end_words": {"type": "string",
                              "description": "The exact last 3-6 words of the teaser (may be in a following block)."},
                "lead_in": {"type": "number", "minimum": 0, "maximum": 3,
                            "description": "Seconds of picture before the first word, e.g. to see the thing appear."},
                "seconds": {"type": "number", "minimum": 3, "maximum": 12},
                "reason": {"type": "string"},
            },
            "required": ["id", "start_words", "end_words", "reason"],
        },
        "cold_open_backups": {
            "type": "array", "maxItems": 2,
            "description": "Only when a cold open is asked for: 1-2 next-best teaser moments "
                           "(different blocks, same fields as cold_open), used if the first pick "
                           "turns out too dark to see.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "start_words": {"type": "string"},
                "end_words": {"type": "string"},
                "lead_in": {"type": "number", "minimum": 0, "maximum": 3},
                "seconds": {"type": "number", "minimum": 3, "maximum": 12},
                "reason": {"type": "string"}},
                "required": ["id", "start_words", "end_words", "reason"]},
        },
    },
    "required": ["titles", "description", "tags", "thumbnail_text", "thumbnail_moments"],
}


@dataclass
class Metadata:
    titles: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_text: list[str] = field(default_factory=list)
    thumbnail_moments: list[str] = field(default_factory=list)
    cold_open: dict | None = None
    cold_open_backups: list[dict] = field(default_factory=list)
    music: list[dict] = field(default_factory=list)


def youtube_chapters(chapters: list[tuple[float, str]], total: float) -> list[tuple[float, str]]:
    """YouTube rules: first at 0:00, at least 3 chapters, each at least 10 seconds."""
    out: list[tuple[float, str]] = []
    for t, title in sorted(chapters):
        if not out:
            out.append((0.0, title))
        elif t - out[-1][0] >= 10 and total - t >= 10:
            out.append((t, title))
    return out if len(out) >= 3 else []


RULES_VERSION = 7     # bump when these rules change, so saved packaging is redone

COLD_OPEN_RULES = (
    "Also pick a cold open: a 4-10 second teaser shown at the very start (in black and white with "
    "'COMING UP'), before the intro. It is the hook, so it must be the single biggest moment of "
    "the episode: a huge reveal or discovery, a near-death, a death, a jump scare, a funny "
    "disaster, a boss or big fight, or the creator's biggest reaction (shouting, 'no way', "
    "'dude'). Prefer the second half. It must be instantly understandable with no context.\n"
    "Never pick walking, travelling, mining, crafting, setup, explanations or calm talk.\n"
    "Viewers must be able to SEE it: avoid moments in dark caves or at night with no light "
    "(check the [on screen] notes); a lit fight beats a pitch-black one. Also list 1-2 "
    "backup moments in cold_open_backups.\n"
    "Pick the exact words: start_words where the excitement starts (skip any 'oh', 'um' or "
    "pause before it) and end_words at the peak of the reaction, before it is resolved or "
    "explained, so viewers stay to see it. Use lead_in when the picture matters before the "
    "first word (the thing appearing on screen). Use the [on screen] notes to check what is "
    "visible. Strongly prefer moments marked [HIGHLIGHT].\n"
    "If nothing in the video is genuinely exciting, leave cold_open out: no teaser is better "
    "than a dull one."
)


def generate(cfg: dict, video: dict, kept: list[Block], caller=llm.call_tool,
             cold_open: bool = False, notes: dict[str, list[str]] | None = None,
             music_library: str = "") -> Metadata:
    lines = []
    for b in kept:
        lines.append(f"{b.id}{' [HIGHLIGHT]' if 'highlight' in b.flags else ''} {b.text}")
        lines += [f"      {n}" for n in (notes or {}).get(b.id, [])]
    system = (
        f"You write YouTube packaging for {cfg['channel'].get('name', 'a Minecraft channel')}, a "
        "Minecraft gaming channel. Match the creator's voice from the transcript. Be specific "
        "about what actually happens in the video; never promise something that doesn't happen."
    )
    if cfg["channel"].get("style_notes"):
        system += "\nCreator's notes: " + cfg["channel"]["style_notes"]
    user = ""
    if video.get("topic"):
        user += f"Video topic: {video['topic']}\n"
    if video.get("series"):
        user += f"Series: {video['series']} {video.get('episode', '')}\n"
    if cold_open:
        user += COLD_OPEN_RULES + "\n"
    schema = SCHEMA
    if music_library:
        from kdcut import music
        user += music.RULES + "\nMusic library:\n" + music_library + "\n"
        schema = {**SCHEMA, "properties": {**SCHEMA["properties"], "music": music.SCHEMA_PART}}
    user += "Final edit transcript:\n" + "\n".join(lines)
    res = caller(cfg, system, user, "submit_metadata", "Submit the YouTube packaging.", schema)
    md = Metadata(**{k: res.get(k, v) for k, v in Metadata().__dict__.items()})
    if not cold_open:
        md.cold_open = None
        md.cold_open_backups = []
    return md


def to_markdown(md: Metadata, chapters: list[tuple[float, str]], frames: list[str]) -> str:
    out = ["# Upload kit", ""]
    if md.titles:
        out += ["## Title options", *[f"{i}. {t}" for i, t in enumerate(md.titles, 1)], ""]
    out += ["## Description", "", md.description or "_(write one)_", ""]
    if chapters:
        out += [*[f"{fmt_ts(t)} {title}" for t, title in chapters], ""]
    else:
        out += ["_(Not enough chapters for YouTube: it needs 3+ chapters, 10s+ each.)_", ""]
    if md.tags:
        out += ["## Tags", ", ".join(md.tags), ""]
    if md.thumbnail_text:
        out += ["## Thumbnail text ideas", *[f"- {t}" for t in md.thumbnail_text], ""]
    if frames:
        out += ["## Thumbnail frame candidates", *[f"- {f}" for f in frames], ""]
    return "\n".join(out)
