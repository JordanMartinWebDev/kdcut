"""KDcut's channel stats as tools Claude can call (a local MCP server over stdio).

The Claude desktop app starts this (`kdcut-mcp.exe`, or `kdcut mcp` from source) and Claude
calls its tools during a chat to read live numbers from YouTube Studio. It only reads: it
can't upload, edit or delete anything. Standard library only: newline-delimited JSON-RPC 2.0.
"""

from __future__ import annotations

import json
import sys
import traceback

from kdcut import __version__

PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "Live YouTube Studio numbers for the creator's channel (KDPlays). Use these instead of asking "
    "for screenshots. Today's numbers are still filling in, so periods end yesterday. Impressions "
    "and CTR come from YouTube's daily reports and lag 1-2 days.")

TOOLS = [
    {"name": "channel_overview",
     "description": "Channel totals for the last N days vs the N days before: views, watch time, "
                    "average view duration, subscribers gained and lost, likes, plus Shorts vs "
                    "long-form split.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Period length in days (default 28).", "minimum": 1}}}},
    {"name": "recent_videos",
     "description": "The latest uploads with their numbers since publishing: views, watch time, "
                    "average view duration and percentage, subscribers gained, likes, comments, "
                    "impressions and click-through rate.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "How many uploads (default 10, max 50)."},
         "kind": {"type": "string", "enum": ["all", "long", "short"],
                  "description": "Only long-form or only Shorts (default all)."}}}},
    {"name": "video_details",
     "description": "Everything about one video: totals since publishing, daily views and subs for "
                    "the last 14 days, traffic sources, the retention curve, impressions and CTR.",
     "inputSchema": {"type": "object", "properties": {
         "video": {"type": "string", "description": "Video id, URL, or words from its title."}},
         "required": ["video"]}},
    {"name": "traffic_sources",
     "description": "Where views came from (Browse, Suggested, Search, Shorts feed, ...) for the "
                    "channel or one video.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Period length in days (default 28)."},
         "video": {"type": "string", "description": "Optional: video id, URL or title words."}}}},
]


def call(name: str, args: dict):
    from kdcut import stats
    if name == "channel_overview":
        return stats.overview(int(args.get("days") or 28))
    if name == "recent_videos":
        try:
            stats.sync_reach()
        except Exception:
            pass                       # impressions are a bonus; the rest still answers
        return stats.video_table(min(int(args.get("limit") or 10), 50), str(args.get("kind") or "all"))
    if name == "video_details":
        try:
            stats.sync_reach()
        except Exception:
            pass
        return stats.video_details(str(args["video"]))
    if name == "traffic_sources":
        vid = stats.find_video(str(args["video"]))["id"] if args.get("video") else ""
        return stats.traffic_sources(int(args.get("days") or 28), vid)
    raise KeyError(name)


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, the response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": want, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "kdcut", "version": __version__}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        try:
            data = call(p.get("name", ""), p.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(data, indent=1, ensure_ascii=False)}]}
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool or argument: {exc}"}}
        except Exception as exc:       # tell Claude what went wrong in plain words
            result = {"content": [{"type": "text", "text": str(exc) or exc.__class__.__name__}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                out = handle(msg) if isinstance(msg, dict) else None
            except Exception:
                traceback.print_exc(file=sys.stderr)
                out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": "Internal error"}}
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
