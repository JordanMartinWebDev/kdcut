"""The story pass: read the whole episode once, before any cut is made.

A two-hour session is edited in windows, so no single editing pass sees the whole arc. This
pass reads the full transcript first and writes a short outline: the episode's goals, the
beats (setup, attempts, setbacks, discoveries, payoffs), and the threads that tie a setup
to its payoff ("needs iron for a shield" ... "the shield saves him from the skeleton").
Every editing window gets the outline, and afterwards any cut that would break a thread
(a kept payoff whose setup was cut) is undone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kdcut.blocks import Block

VERSION = 1

KINDS = ["intro", "goal", "setup", "attempt", "setback", "discovery", "danger", "payoff",
         "personal", "aside", "housekeeping", "outro"]

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentences: what this episode is about and how it ends."},
        "goals": {"type": "array", "items": {"type": "string"},
                  "description": "What the creator sets out to do, in order (1-5 short items)."},
        "beats": {
            "type": "array",
            "description": "The episode in order, as 8-40 beats covering every block.",
            "items": {"type": "object", "properties": {
                "from": {"type": "string", "description": "First block id of the beat."},
                "to": {"type": "string", "description": "Last block id of the beat."},
                "kind": {"type": "string", "enum": KINDS},
                "what": {"type": "string", "description": "One short line on what happens."},
                "matters": {"type": "integer", "minimum": 1, "maximum": 3,
                            "description": "3 = the story needs it, 2 = good, 1 = could go."},
            }, "required": ["from", "to", "kind", "what", "matters"]},
        },
        "threads": {
            "type": "array",
            "description": "Setups that pay off later. Only real ones, where cutting the setup "
                           "would make the payoff confusing.",
            "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "setup": {"type": "array", "items": {"type": "string"}, "description": "Block ids (1-3)."},
                "payoff": {"type": "array", "items": {"type": "string"}, "description": "Block ids (1-3)."},
            }, "required": ["name", "setup", "payoff"]},
        },
    },
    "required": ["summary", "beats"],
}

SYSTEM = (
    "You are the story editor of a Minecraft let's play. Before anything is cut, you read the "
    "whole raw session and outline the story the finished episode should tell: what the "
    "creator wants, what they try, what goes wrong, what they find, how it pays off. Personal "
    "stories and jokes are part of the story (kind 'personal'), not filler. You don't cut "
    "anything yourself; your outline guides the editor."
)


@dataclass
class Outline:
    summary: str = ""
    goals: list[str] = field(default_factory=list)
    beats: list[dict] = field(default_factory=list)
    threads: list[dict] = field(default_factory=list)

    def text(self) -> str:
        """The outline as the editor sees it in every window."""
        out = [f"Story: {self.summary}"]
        if self.goals:
            out.append("Goals: " + "; ".join(self.goals))
        out.append("Beats (matters 3 = the story needs it):")
        for b in self.beats:
            out.append(f"- {b.get('from')}-{b.get('to')} {b.get('kind')} (matters {b.get('matters')}): {b.get('what')}")
        if self.threads:
            out.append("Threads (keep the setup when the payoff is kept):")
            for t in self.threads:
                out.append(f"- {t.get('name')}: setup {', '.join(t.get('setup', []))} -> "
                           f"payoff {', '.join(t.get('payoff', []))}")
        return "\n".join(out)


def key(blocks: list[Block]) -> str:
    sig = f"v{VERSION}|" + "|".join(f"{b.id}:{b.text[:40]}" for b in blocks)
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]


def outline(cfg: dict, video: dict, blocks: list[Block], caller, cache: Path | None = None,
            log=print, force: bool = False) -> Outline:
    k = key(blocks)
    if cache is not None and cache.is_file() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == k:
                return Outline(**data["outline"])
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            pass
    log("Reading the whole episode for the story")
    lines = []
    last_src = None
    for b in blocks:
        if b.source != last_src:
            lines.append(f"--- Recording {b.source + 1} ---")
            last_src = b.source
        if "forced_drop" in b.flags:
            continue
        lines.append(f"{b.id} {b.text}")
    user = ""
    if video.get("topic"):
        user += f"What the creator says this episode is: {video['topic']}\n"
    if video.get("series"):
        user += f"Series: {video['series']} {video.get('episode', '')}\n"
    if cfg["channel"].get("style_notes"):
        user += f"Creator's notes: {cfg['channel']['style_notes']}\n"
    user += "\nFull raw transcript:\n" + "\n".join(lines)
    res = caller(cfg, SYSTEM, user, "submit_outline", "Submit the story outline.", SCHEMA)
    ids = {b.id for b in blocks}
    o = Outline(summary=str(res.get("summary", "")),
                goals=[str(g) for g in res.get("goals", [])][:6],
                beats=[b for b in res.get("beats", []) if isinstance(b, dict)
                       and b.get("from") in ids and b.get("to") in ids],
                threads=[{"name": str(t.get("name", "")),
                          "setup": [i for i in t.get("setup", []) if i in ids][:3],
                          "payoff": [i for i in t.get("payoff", []) if i in ids][:3]}
                         for t in res.get("threads", []) if isinstance(t, dict)])
    o.threads = [t for t in o.threads if t["setup"] and t["payoff"]]
    if cache is not None:
        try:
            cache.write_text(json.dumps({"key": k, "outline": asdict(o)}, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        except OSError:
            pass
    return o


def protect_threads(plan, outline: Outline, blocks: list[Block]) -> list[str]:
    """Undo cuts of a setup whose payoff stays in the video. Returns the restored ids."""
    restored = []
    for t in outline.threads:
        if all(p in plan.drop for p in t["payoff"]):
            continue                    # the payoff is gone too: the setup can go
        for sid in t["setup"]:
            if sid in plan.drop:
                plan.drop.pop(sid)
                restored.append(sid)
    if restored:
        plan.notes.append(f"Kept {len(restored)} set-up block(s) the AI wanted to cut, because their "
                          "payoff stays in the video: " + ", ".join(restored))
    return restored


def beat_of(outline: Outline, blocks: list[Block]) -> dict[str, dict]:
    """block id -> its beat."""
    order = {b.id: i for i, b in enumerate(blocks)}
    out = {}
    for beat in outline.beats:
        a, z = order.get(beat["from"]), order.get(beat["to"])
        if a is None or z is None:
            continue
        for b in blocks[min(a, z):max(a, z) + 1]:
            out.setdefault(b.id, beat)
    return out
