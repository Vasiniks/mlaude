#!/usr/bin/env python3
"""Offline unit tests for muse_responses.py (the Meta translation layer).

Stdlib only, no network, no API key. Run from the repo root or router/:

    python3 router/test_translator.py
    python3 test_translator.py        # from inside router/
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import muse_responses as mr
import router as router_mod


def parse_sse(chunks):
    """Turn a list of raw SSE byte-chunks into (event, payload) pairs."""
    events = []
    for c in chunks:
        name, payload = None, None
        for line in c.decode().splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
        events.append((name, payload))
    return events


class FixSchemaTests(unittest.TestCase):
    def test_nul_escape_rewritten(self):
        node = {"type": "string", "pattern": "a\\0b"}
        self.assertTrue(router_mod.fix_schema(node))
        self.assertEqual(node["pattern"], "a\\x00b")

    def test_clean_pattern_untouched(self):
        node = {"type": "string", "pattern": "^[a-z]+$"}
        self.assertFalse(router_mod.fix_schema(node))
        self.assertEqual(node["pattern"], "^[a-z]+$")

    def test_data_keywords_not_rewritten(self):
        for kw in ("enum", "const", "default", "examples"):
            node = {kw: ["a\\0b"] if kw == "enum" else "a\\0b"}
            self.assertFalse(router_mod.fix_schema(node), kw)
            self.assertIn("\\0", json.dumps(node))


class StripTtlTests(unittest.TestCase):
    def test_ttl_removed(self):
        blocks = [{"type": "text", "text": "hi",
                   "cache_control": {"type": "ephemeral", "ttl": "5m"}}]
        self.assertTrue(router_mod.strip_ttl(blocks))
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_no_ttl_no_change(self):
        blocks = [{"type": "text", "text": "hi"}]
        self.assertFalse(router_mod.strip_ttl(blocks))

    def test_nested_tool_result(self):
        blocks = [{"type": "tool_result", "tool_use_id": "u1",
                   "content": [{"type": "text", "text": "out",
                                "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}]
        self.assertTrue(router_mod.strip_ttl(blocks))
        self.assertNotIn("ttl", blocks[0]["content"][0]["cache_control"])


class NormalizeTests(unittest.TestCase):
    def test_stop_sequences_dropped(self):
        data = {"model": "muse-spark-1.3", "max_tokens": 5,
                "stop_sequences": ["x"],
                "messages": [{"role": "user", "content": "hi"}]}
        self.assertTrue(mr.normalize_for_messages(data))
        self.assertNotIn("stop_sequences", data)

    def test_server_tool_dropped_and_choice_reset(self):
        tool = {"type": "web_search_20260201", "name": "web_search"}
        data = {"model": "muse-spark-1.3", "max_tokens": 5,
                "tools": [tool, {"name": "ok_tool",
                                 "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "any", "name": "web_search"},
                "messages": [{"role": "user", "content": "hi"}]}
        self.assertTrue(mr.normalize_for_messages(data))
        self.assertEqual([t["name"] for t in data["tools"]], ["ok_tool"])
        self.assertEqual(data["tool_choice"], {"type": "auto"})

    def test_extension_block_mapped_to_text(self):
        data = {"model": "muse-spark-1.3", "max_tokens": 5,
                "messages": [{"role": "user", "content": [
                    {"type": "tool_addition",
                     "content": [{"name": "helper",
                                  "input_schema": {"type": "object"}}]}]}]}
        self.assertTrue(mr.normalize_for_messages(data))
        content = data["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        # The tool definition carried inside is merged into tools.
        self.assertTrue(any(t.get("name") == "helper" for t in data["tools"]))


class ToResponsesTests(unittest.TestCase):
    def test_minimal_text_turn(self):
        data = {"model": "muse-spark-1.3", "max_tokens": 5,
                "messages": [{"role": "user", "content": "hi"}]}
        body, summarize = mr.to_responses(data)
        self.assertFalse(summarize)
        self.assertEqual(body["model"], "muse-spark-1.3")
        self.assertFalse(body["store"])
        self.assertEqual(body["prompt_cache_key"], "claude-code")
        self.assertFalse(body["stream"])
        item = body["input"][0]
        self.assertEqual(item["role"], "user")
        self.assertEqual(item["content"],
                         [{"type": "input_text", "text": "hi"}])

    def test_system_string_becomes_instructions(self):
        data = {"model": "muse-spark-1.3", "max_tokens": 5,
                "system": "be nice",
                "messages": [{"role": "user", "content": "hi"}]}
        body, _ = mr.to_responses(data)
        self.assertEqual(body["instructions"], "be nice")

    def test_unsupported_fields_raise(self):
        for field in ("stop_sequences", "top_k", "container", "mcp_servers"):
            data = {"model": "muse-spark-1.3", "max_tokens": 5,
                    field: ["x"] if field == "stop_sequences" else True,
                    "messages": [{"role": "user", "content": "hi"}]}
            with self.assertRaises(mr.Unsupported, msg=field):
                mr.to_responses(data)


class FromResponseTests(unittest.TestCase):
    def test_full_turn(self):
        resp = {"id": "r1", "status": "completed",
                "output": [
                    {"type": "reasoning", "encrypted_content": "enc",
                     "summary": [{"text": "thinking out loud"}]},
                    {"type": "message",
                     "content": [{"type": "output_text", "text": "hello"}]},
                    {"type": "function_call", "call_id": "c1",
                     "name": "get_time", "arguments": "{}"}],
                "usage": {"input_tokens": 100, "output_tokens": 10,
                          "input_tokens_details": {"cached_tokens": 30}}}
        msg = mr.from_response(resp, "muse-spark-1.3", summarize=True)
        self.assertEqual(msg["type"], "message")
        self.assertEqual(msg["stop_reason"], "tool_use")
        kinds = [b["type"] for b in msg["content"]]
        self.assertEqual(kinds, ["thinking", "text", "tool_use"])
        self.assertEqual(msg["content"][0]["signature"], "enc")
        self.assertEqual(msg["content"][2]["name"], "get_time")
        self.assertEqual(msg["usage"]["input_tokens"], 70)
        self.assertEqual(msg["usage"]["cache_read_input_tokens"], 30)

    def test_redacted_when_not_summarizing(self):
        resp = {"id": "r2", "status": "completed",
                "output": [{"type": "reasoning", "encrypted_content": "enc"}],
                "usage": {}}
        msg = mr.from_response(resp, "muse-spark-1.3", summarize=False)
        self.assertEqual(msg["content"][0]["type"], "redacted_thinking")
        self.assertEqual(msg["stop_reason"], "end_turn")

    def test_incomplete_means_max_tokens(self):
        resp = {"id": "r3", "status": "incomplete", "output": [], "usage": {}}
        msg = mr.from_response(resp, "muse-spark-1.3", summarize=False)
        self.assertEqual(msg["stop_reason"], "max_tokens")


class StreamTranslatorTests(unittest.TestCase):
    def feed(self, tr, events):
        out = []
        for e in events:
            out += parse_sse(tr.handle(e))
        return out

    def test_text_stream(self):
        tr = mr.StreamTranslator("muse-spark-1.3", summarize=False)
        events = self.feed(tr, [
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "m1", "type": "message"}},
            {"type": "response.output_text.delta", "output_index": 0,
             "delta": "hel"},
            {"type": "response.output_text.delta", "output_index": 0,
             "delta": "lo"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "message"}},
            {"type": "response.completed",
             "response": {"status": "completed", "usage": {}}},
        ])
        names = [n for n, _ in events]
        self.assertEqual(names, ["message_start", "content_block_start",
                                 "content_block_delta", "content_block_delta",
                                 "content_block_stop", "message_delta",
                                 "message_stop"])
        deltas = [p["delta"]["text"] for n, p in events
                  if n == "content_block_delta"]
        self.assertEqual("".join(deltas), "hello")
        delta = [p for n, p in events if n == "message_delta"][0]
        self.assertEqual(delta["delta"]["stop_reason"], "end_turn")
        self.assertTrue(tr.finished)

    def test_tool_stream_stop_reason(self):
        tr = mr.StreamTranslator("muse-spark-1.3", summarize=False)
        events = self.feed(tr, [
            {"type": "response.created", "response": {"id": "r2"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "f1", "type": "function_call",
                      "call_id": "c1", "name": "get_time"}},
            {"type": "response.function_call_arguments.delta",
             "output_index": 0, "delta": "{}"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "function_call", "arguments": "{}"}},
            {"type": "response.completed",
             "response": {"status": "completed", "usage": {}}},
        ])
        names = [n for n, _ in events]
        self.assertIn("content_block_start", names)
        start = [p for n, p in events if n == "content_block_start"][0]
        self.assertEqual(start["content_block"]["name"], "get_time")
        delta = [p for n, p in events if n == "message_delta"][0]
        self.assertEqual(delta["delta"]["stop_reason"], "tool_use")


class ErrorMappingTests(unittest.TestCase):
    def test_long_prompt_maps_to_compact_signal(self):
        payload = json.loads(mr._anthropic_error(
            400, b'{"error":{"message":"context window too long"}}').decode())
        self.assertIn("prompt is too long", payload["error"]["message"])
        self.assertEqual(payload["error"]["type"], "invalid_request_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
