"""Minimal Claude API client (standard library only) with structured (tool) output."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"


class LLMError(RuntimeError):
    pass


def api_key(cfg: dict) -> str | None:
    """The key saved in the app's settings wins; otherwise the environment variable."""
    return (cfg["llm"].get("api_key") or "").strip() or \
        os.environ.get(cfg["llm"].get("api_key_env", "ANTHROPIC_API_KEY"))


def image_block(path, max_side: int = 768) -> dict:
    """An image content block for the API (JPEG, resized so the long side <= max_side)."""
    import base64
    import io

    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode("ascii")}}


def call_tool(cfg: dict, system: str, user, tool_name: str, tool_description: str,
              schema: dict, retries: int = 3) -> dict:
    """Send one prompt and force the model to answer by calling `tool_name`.

    `user` is text, or a list of content blocks (text and images).
    Returns the tool input (a dict matching `schema`).
    """
    key = api_key(cfg)
    if not key:
        raise LLMError(
            "No Claude API key. Add one in Settings (get a key at console.anthropic.com), "
            "or turn off the AI edit."
        )
    model = cfg["llm"]["model"]
    tools = [{"name": tool_name, "description": tool_description, "input_schema": schema}]
    messages = [{"role": "user", "content": user}]
    forced = model not in _NO_FORCED_TOOLS
    nudged = False
    while True:
        body = {
            "model": model,
            "max_tokens": int(cfg["llm"].get("max_tokens", 16000)),
            "system": system if forced else
            f"{system}\n\nGive your answer only by calling the {tool_name} tool, exactly once.",
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "tool", "name": tool_name} if forced else {"type": "auto"},
        }
        try:
            payload = _post(key, body, retries)
        except _ToolChoiceUnsupported:
            # Some models (e.g. ones that always think first) refuse a forced tool call.
            # Ask nicely instead, and remember for the rest of the session.
            _NO_FORCED_TOOLS.add(model)
            forced = False
            continue
        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                return block["input"]
        if payload.get("stop_reason") == "max_tokens":
            raise LLMError("The AI's answer was cut off (max_tokens). Lower llm.window_blocks.")
        found = _json_in_text(payload)
        if found is not None:
            return found
        if nudged:
            raise LLMError("The AI did not return a structured answer.")
        nudged = True
        messages = messages + [
            {"role": "assistant", "content": payload.get("content") or [{"type": "text", "text": "..."}]},
            {"role": "user", "content": f"Please give the answer now by calling the {tool_name} tool."},
        ]


_NO_FORCED_TOOLS: set[str] = set()


class _ToolChoiceUnsupported(Exception):
    pass


def _post(key: str, body: dict, retries: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(API_URL, data=data, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 400 and "tool_choice" in detail and body["tool_choice"]["type"] != "auto":
                raise _ToolChoiceUnsupported() from exc
            if exc.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise LLMError(f"Claude API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise LLMError(f"Could not reach the Claude API: {exc.reason}") from exc
    raise LLMError("Could not reach the Claude API.")


def _json_in_text(payload: dict) -> dict | None:
    """Fallback: the answer written out as JSON text instead of a tool call."""
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        got = json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        return None
    return got if isinstance(got, dict) else None


def check_key(key: str) -> tuple[bool, str]:
    """Quick validity check for the Settings window (lists models; costs nothing)."""
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1", headers={
        "x-api-key": key.strip(), "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True, "Key works."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "That key was rejected. Copy it again from console.anthropic.com."
        return False, f"The API answered with error {exc.code}. Try again in a minute."
    except urllib.error.URLError as exc:
        return False, f"Couldn't reach the Claude API ({exc.reason}). Check your internet."
