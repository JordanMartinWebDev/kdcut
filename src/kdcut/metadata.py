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


def youtube_chapters(chapters: list[tuple[float, str]], total: float) -> list[tuple[float, str]]:
    """YouTube rules: first at 0:00, at least 3 chapters, each at least 10 seconds."""
    out: list[tuple[float, str]] = []
    for t, title in sorted(chapters):
        if not out:
            out.append((0.0, title))
        elif t - out[-1][0] >= 10 and total - t >= 10:
            out.append((t, title))
    return out if len(out) >= 3 else []


def generate(cfg: dict, video: dict, kept: list[Block], caller=llm.call_tool) -> Metadata:
    lines = [f"{b.id} {b.text}" for b in kept]
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
    user += "Final edit transcript:\n" + "\n".join(lines)
    res = caller(cfg, system, user, "submit_metadata", "Submit the YouTube packaging.", SCHEMA)
    return Metadata(**{k: res.get(k, v) for k, v in Metadata().__dict__.items()})


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
