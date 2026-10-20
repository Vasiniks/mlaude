#!/usr/bin/env python3
"""Tests for router/oc_chat.py — Anthropic<->OpenAI chat translator for oc- models.

Stdlib only, no network. Mirrors test_translator.py style.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oc_chat


def parse_sse(chunks):
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


def req(**kw):
    base = {"model": "oc-beam-free", "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return base


class ToOpenAITests(unittest.TestCase):
    def test_strips_prefix_and_defaults(self):
        body, upstream = oc_chat.to_openai(req())
        self.assertEqual(upstream, "beam-free")
        self.assertEqual(body["model"], "beam-free")
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_tokens"], 5)

    def test_system_string_first(self):
        body, _ = oc_chat.to_openai(req(system="be nice"))
        self.assertEqual(body["messages"][0],
                         {"role": "system", "content": "be nice"})
        self.assertEqual(body["messages"][1]["role"], "user")

    def test_text_turn(self):
        body, _ = oc_chat.to_openai(req())
        self.assertEqual(body["messages"][-1],
                         {"role": "user",
                          "content": [{"type": "text", "text": "hi"}]})

    def test_tool_use_becomes_tool_calls(self):
        data = req(messages=[
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "c1", "name": "get_time",
                 "input": {"tz": "UTC"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1",
                 "content": "12:00"}]}])
        body, _ = oc_chat.to_openai(data)
        asst = body["messages"][0]
        self.assertEqual(asst["role"], "assistant")
        self.assertEqual(asst["tool_calls"],
                         [{"id": "c1", "type": "function",
                           "function": {"name": "get_time",
                                        "arguments": '{"tz":"UTC"}'}}])
        tool = body["messages"][1]
        self.assertEqual(tool, {"role": "tool", "tool_call_id": "c1",
                                "content": "12:00"})

    def test_stop_sequences_become_stop(self):
        body, _ = oc_chat.to_openai(req(stop_sequences=["halt"]))
        self.assertEqual(body["stop"], ["halt"])

    def test_thinking_dropped_not_fatal(self):
        data = req(messages=[
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm",
                 "signature": "enc"},
                {"type": "text", "text": "done"}]}])
        body, _ = oc_chat.to_openai(data)
        asst = body["messages"][0]
        texts = [p.get("text", "") for p in asst["content"]
                 if p.get("type") == "text"]
        self.assertEqual(texts, ["done"])

    def test_image_becomes_image_url(self):
        data = req(messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png",
                        "data": "AAA"}}]}])
        body, _ = oc_chat.to_openai(data)
        part = body["messages"][-1]["content"][0]
        self.assertEqual(part["type"], "image_url")
        self.assertTrue(part["image_url"]["url"].startswith(
            "data:image/png;base64,AAA"))

    def test_tools_mapped(self):
        data = req(tools=[{"name": "get_time",
                           "description": "clock",
                           "input_schema": {"type": "object"}}])
        body, _ = oc_chat.to_openai(data)
        self.assertEqual(body["tools"],
                         [{"type": "function",
                           "function": {"name": "get_time",
                                        "description": "clock",
                                        "parameters": {"type": "object"}}}])

    def test_non_oc_model_rejected(self):
        with self.assertRaises(oc_chat.Unsupported):
            oc_chat.to_openai(req(model="muse-spark-1.3"))


class FromOpenAITests(unittest.TestCase):
    def test_text_and_tool(self):
        resp = {"id": "chat1",
                "choices": [{"message": {
                    "role": "assistant", "content": "hello",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "get_time",
                                                 "arguments": "{}"}}]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "prompt_tokens_details": {"cached_tokens": 30}}}
        msg = oc_chat.from_openai(resp, "oc-beam-free")
        kinds = [b["type"] for b in msg["content"]]
        self.assertEqual(kinds, ["text", "tool_use"])
        self.assertEqual(msg["content"][1]["name"], "get_time")
        self.assertEqual(msg["stop_reason"], "tool_use")
        self.assertEqual(msg["usage"]["input_tokens"], 70)
        self.assertEqual(msg["usage"]["cache_read_input_tokens"], 30)

    def test_length_means_max_tokens(self):
        resp = {"id": "c2",
                "choices": [{"message": {"role": "assistant",
                                         "content": "half"},
                             "finish_reason": "length"}],
                "usage": {}}
        msg = oc_chat.from_openai(resp, "oc-beam-free")
        self.assertEqual(msg["stop_reason"], "max_tokens")


class StreamTests(unittest.TestCase):
    def feed(self, tr, events):
        out = []
        for e in events:
            out += parse_sse(tr.handle(e))
        return out

    def test_text_stream(self):
        tr = oc_chat.StreamTranslator("oc-beam-free")
        events = self.feed(tr, [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])
        names = [n for n, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertIn("content_block_start", names)
        self.assertIn("message_stop", names)
        deltas = [p["delta"]["text"] for n, p in events
                  if n == "content_block_delta"]
        self.assertEqual("".join(deltas), "hello")
        delta = [p for n, p in events if n == "message_delta"][0]
        self.assertEqual(delta["delta"]["stop_reason"], "end_turn")
        self.assertTrue(tr.finished)

    def test_tool_then_text_block_indices(self):
        tr = oc_chat.StreamTranslator("oc-beam-free")
        events = self.feed(tr, [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1",
                 "function": {"name": "get_time", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"content": "done"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])
        starts = [p for n, p in events if n == "content_block_start"]
        self.assertEqual(
            [(p["index"], p["content_block"]["type"]) for p in starts],
            [(0, "tool_use"), (1, "text")])
        deltas = [p for n, p in events if n == "content_block_delta"]
        self.assertEqual(
            sorted(p["index"] for p in deltas), [1])
        stops = sorted(p["index"] for n, p in events
                       if n == "content_block_stop")
        self.assertEqual(stops, [0, 1])

    def test_tool_call_stream(self):
        tr = oc_chat.StreamTranslator("oc-beam-free")
        events = self.feed(tr, [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1",
                 "function": {"name": "get_time", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "{}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])
        names = [n for n, _ in events]
        self.assertIn("content_block_start", names)
        start = [p for n, p in events
                 if n == "content_block_start"][0]
        self.assertEqual(start["content_block"]["name"], "get_time")
        self.assertEqual(start["content_block"]["id"], "c1")
        delta = [p for n, p in events if n == "message_delta"][0]
        self.assertEqual(delta["delta"]["stop_reason"], "tool_use")


class ErrorMappingTests(unittest.TestCase):
    def test_long_prompt_signal(self):
        payload = json.loads(oc_chat._openai_error(
            400, b'{"error":{"message":"context length exceeded"}}').decode())
        self.assertIn("prompt is too long", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
