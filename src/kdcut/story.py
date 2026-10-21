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

VERSION = 3

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
        "must_keep": {
            "type": "array",
            "description": "Lines the story can't lose: each part of a checklist the creator ticks "
                           "off ('enchanting table, check' ... 'bookshelves ... also check'), the line "
                           "that explains why something happens (a death, a trip back), reveals and "
                           "their set-ups. Only the important ones.",
            "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "why": {"type": "string"}}, "required": ["id", "why"]},
        },
        "callbacks": {
            "type": "array",
            "description": "Running gags and recurring check-ins that come back through the episode "
                           "('as is tradition, let's check the wheat farm'). Every block where it comes up.",
            "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}},
            }, "required": ["name", "ids"]},
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
    "anything yourself; your outline guides the editor.\n"
    "The creator records short clips on purpose and narrates for the edit. He leaves hard clues: "
    "checklists ('gear check, nether check'), a sentence spread over several clips ('And as for "
    "bookshelves.' / 'They are also' / 'a check.') with the footage in between showing it, 'let's "
    "go' at the end of a plan, 'there's footage on screen now'. Those are the spine: list them "
    "in must_keep, along with each build's payoff (the 'it's done' line, the test, the loot). "
    "Running gags that come back (a check-in on the same farm every so often) go in callbacks. "
    "[on screen] notes say what the footage shows when he isn't talking (a death "
    "screen, a discovery); a death that changes what happens next is a beat."
)


@dataclass
class Outline:
    summary: str = ""
    goals: list[str] = field(default_factory=list)
    beats: list[dict] = field(default_factory=list)
    threads: list[dict] = field(default_factory=list)
    must_keep: list[dict] = field(default_factory=list)
    callbacks: list[dict] = field(default_factory=list)

    def text(self) -> str:
        """The outline as the editor sees it in every window."""
        out = [f"Story: {self.summary}"]
        if self.goals:
            out.append("Goals: " + "; ".join(self.goals))
        out.append("Beats (matters 3 = the story needs it):")
        for b in self.beats:
            out.append(f"- {b.get('from')}-{b.get('to')} {b.get('kind')} (matters {b.get('matters')}): {b.get('what')}")
        if self.must_keep:
            out.append("Must keep (the story breaks without these): "
                       + "; ".join(f"{m['id']} ({m.get('why', '')})" for m in self.must_keep))
        for c in self.callbacks:
            out.append(f"Running gag '{c['name']}' (keep every time): " + ", ".join(c["ids"]))
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
            log=print, force: bool = False, notes: dict[str, list[str]] | None = None,
            durations: list[float] | None = None) -> Outline:
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
            length = f" ({durations[b.source]:.0f}s clip)" if durations and b.source < len(durations) else ""
            lines.append(f"--- Recording {b.source + 1}{length} ---")
            last_src = b.source
        if "forced_drop" in b.flags:
            continue
        lines.append(f"{b.id} {b.text}")
        lines += [f"      {n}" for n in (notes or {}).get(b.id, [])]
    user = ""
    if video.get("topic"):
        user += f"What the creator says this episode is: {video['topic']}\n"
    if video.get("series"):
        user += f"Series: {video['series']} {video.get('episode', '')}\n"
    if cfg["channel"].get("style_notes"):
        user += f"Creator's notes: {cfg['channel']['style_notes']}\n"
    user += "\nFull raw transcript:\n" + "\n".join(lines)
    ids = {b.id for b in blocks}
    fix = id_fixer(ids)
    o = Outline()
    for attempt in range(2):
        res = caller(cfg, SYSTEM, user, "submit_outline", "Submit the story outline.", SCHEMA)
        o = _read(res, fix)
        if o.beats:
            break
        log("The story outline came back without beats; asking again")
    if not o.beats:
        log("Warning: no story outline beats, so nothing protects the story's set-ups this run")
    o.threads = [t for t in o.threads if t["setup"] and t["payoff"]]
    if cache is not None and o.beats:
        try:
            cache.write_text(json.dumps({"key": k, "outline": asdict(o)}, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        except OSError:
            pass
    return o


def id_fixer(ids: set[str]):
    """Read block ids the way the AI writes them: 'B0005', 'B5', 'b0005', 'B0005-B0009'."""
    width = max((len(i) - 1 for i in ids), default=4)

    def fix(v) -> str | None:
        v = str(v or "").strip().split()[0].strip(",.:;") if str(v or "").strip() else ""
        v = v.split("-")[0] if v.count("-") == 1 and v.split("-")[1].upper().startswith("B") else v
        if v in ids:
            return v
        digits = "".join(ch for ch in v if ch.isdigit())
        if v[:1].upper() == "B" and digits:
            cand = "B" + digits.zfill(width)
            return cand if cand in ids else None
        return None
    return fix


def _read(res: dict, fix) -> Outline:
    beats = []
    for b in res.get("beats", []) or []:
        if not isinstance(b, dict):
            continue
        a, z = fix(b.get("from")), fix(b.get("to"))
        if a and z:
            beats.append({**b, "from": a, "to": z})
    threads = []
    for t in res.get("threads", []) or []:
        if isinstance(t, dict):
            threads.append({"name": str(t.get("name", "")),
                            "setup": [x for x in map(fix, t.get("setup", []) or []) if x][:3],
                            "payoff": [x for x in map(fix, t.get("payoff", []) or []) if x][:3]})
    must = [{"id": fix(m.get("id")), "why": str(m.get("why", ""))[:80]}
            for m in res.get("must_keep", []) or [] if isinstance(m, dict)]
    calls = []
    for c in res.get("callbacks", []) or []:
        if isinstance(c, dict):
            ids = [x for x in map(fix, c.get("ids", []) or []) if x]
            if len(ids) >= 2:
                calls.append({"name": str(c.get("name", ""))[:60], "ids": ids[:12]})
    return Outline(summary=str(res.get("summary", "")), goals=[str(g) for g in res.get("goals", []) or []][:6],
                   beats=beats, threads=threads, must_keep=[m for m in must if m["id"]][:60],
                   callbacks=calls[:8])


def cached(cache: Path, blocks: list[Block]) -> Outline | None:
    """The saved outline for these blocks, or None (never calls the AI)."""
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("key") == key(blocks):
            return Outline(**data["outline"])
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


def protect_threads(plan, outline: Outline, blocks: list[Block]) -> list[str]:
    """Undo cuts of a setup whose payoff stays in the video. Returns the restored ids."""
    restored = []
    known = {b.id for b in blocks}
    for bid in [m["id"] for m in outline.must_keep] + [i for c in outline.callbacks for i in c["ids"]]:
        if bid in plan.drop and bid in known:
            plan.drop.pop(bid)
            restored.append(bid)
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
