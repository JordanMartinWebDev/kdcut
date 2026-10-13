"""Minimal Claude API client (standard library only) with forced tool output."""

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


def call_tool(cfg: dict, system: str, user: str, tool_name: str, tool_description: str,
              schema: dict, retries: int = 3) -> dict:
    """Send one prompt and force the model to answer by calling `tool_name`.

    Returns the tool input (a dict matching `schema`).
    """
    key = api_key(cfg)
    if not key:
        raise LLMError(
            "No Claude API key. Add one in Settings (get a key at console.anthropic.com), "
            "or turn off the AI edit."
        )
    body = {
        "model": cfg["llm"]["model"],
        "max_tokens": int(cfg["llm"].get("max_tokens", 16000)),
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [{"name": tool_name, "description": tool_description, "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": tool_name},
    }
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(API_URL, data=data, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise LLMError(f"Claude API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise LLMError(f"Could not reach the Claude API: {exc.reason}") from exc

    for block in payload.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block["input"]
    if payload.get("stop_reason") == "max_tokens":
        raise LLMError("The AI's answer was cut off (max_tokens). Lower llm.window_blocks.")
    raise LLMError("The AI did not return a structured answer.")


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
