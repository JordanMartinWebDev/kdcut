import io
import json
import unittest
import urllib.error
from unittest import mock

from kdcut import llm

CFG = {"llm": {"model": "m-thinks", "api_key": "k"}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(content, stop="tool_use"):
    return Resp(json.dumps({"content": content, "stop_reason": stop}).encode())


def bad_request(msg):
    return urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": msg}}).encode()))


class ToolChoiceFallbackTests(unittest.TestCase):
    def setUp(self):
        llm._NO_FORCED_TOOLS.clear()

    def test_model_that_refuses_forced_tools(self):
        sent = []

        def fake(req, timeout=0):
            body = json.loads(req.data)
            sent.append(body)
            if body["tool_choice"]["type"] == "tool":
                raise bad_request('tool_choice: type "tool" and "any" are not supported for this model.')
            if len(body["messages"]) == 1:
                return ok([{"type": "thinking", "thinking": "hmm", "signature": "s"},
                           {"type": "text", "text": "Let me think."}], "end_turn")
            return ok([{"type": "tool_use", "name": "plan", "id": "t1", "input": {"keep": [1]}}])

        with mock.patch("urllib.request.urlopen", fake):
            out = llm.call_tool(CFG, "sys", "hi", "plan", "d", {"type": "object"})
        self.assertEqual(out, {"keep": [1]})
        self.assertEqual(sent[1]["tool_choice"], {"type": "auto"})
        self.assertIn("calling the plan tool", sent[1]["system"])
        self.assertEqual(sent[2]["messages"][1]["role"], "assistant")   # nudged once
        # Remembered: the next call goes straight to auto.
        with mock.patch("urllib.request.urlopen", fake):
            llm.call_tool(CFG, "sys", "hi", "plan", "d", {"type": "object"})
        self.assertEqual(sent[3]["tool_choice"], {"type": "auto"})

    def test_json_written_as_text_is_accepted(self):
        llm._NO_FORCED_TOOLS.add("m-thinks")
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=0: ok([{"type": "text", "text": 'Here: {"a": 1}'}], "end_turn")):
            self.assertEqual(llm.call_tool(CFG, "s", "u", "plan", "d", {}), {"a": 1})

    def test_other_errors_still_raise(self):
        def fake(req, timeout=0):
            raise bad_request("messages: bad")
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(llm.LLMError):
                llm.call_tool(CFG, "s", "u", "plan", "d", {})


if __name__ == "__main__":
    unittest.main()
