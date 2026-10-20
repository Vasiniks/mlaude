#!/usr/bin/env python3
"""Tests for router/oc_local.py — local OpenCode service transport.

Stdlib only, no network, no subprocess (helpers are pure).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oc_local


class FlattenTests(unittest.TestCase):
    def test_system_plus_turns(self):
        data = {"model": "oc-glm-5.3-flash-free", "system": "be nice",
                "messages": [{"role": "user", "content": "hi"}]}
        text = oc_local.flatten_to_text(data)
        self.assertIn("be nice", text)
        self.assertIn("user: hi", text)

    def test_tool_result_summarized(self):
        data = {"model": "oc-x", "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "get_time",
                 "input": {"tz": "UTC"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1",
                 "content": "12:00"}]}]}
        text = oc_local.flatten_to_text(data)
        self.assertIn("get_time", text)
        self.assertIn("12:00", text)

    def test_thinking_dropped(self):
        data = {"model": "oc-x", "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "done"}]}]}
        self.assertEqual(oc_local.flatten_to_text(data), "assistant: done")

    def test_empty(self):
        self.assertEqual(oc_local.flatten_to_text({"model": "oc-x",
                                                   "messages": []}), "")


class AssistantTextTests(unittest.TestCase):
    def _msgs(self):
        return [
            {"type": "user", "time": {"created": 100}, "text": "hi"},
            {"type": "assistant", "time": {"created": 50},  # stale
             "content": [{"type": "text", "text": "OLD"}]},
            {"type": "assistant", "time": {"created": 150},
             "content": [{"type": "reasoning", "text": "r"},
                         {"type": "text", "text": "NEW"}]},
        ]

    def test_only_fresh_text(self):
        self.assertEqual(oc_local._assistant_text(self._msgs(), 100), "NEW")

    def test_nothing_fresh(self):
        self.assertEqual(oc_local._assistant_text(self._msgs(), 999), "")


class ErrorKindTests(unittest.TestCase):
    def test_failed_outcome_is_model_error(self):
        calls = {"n": 0}

        def fake_run(*args):
            calls["n"] += 1
            if args[0] == "session.create":
                return {"data": {"id": "ses_test"}}
            if args[0] == "session.message.list":
                return {"data": [
                    {"type": "idle", "time": {"created": 9999999999999},
                     "outcome": "failed",
                     "error": {"message": "Rate limit exceeded."}}]}
            return {"data": {}}

        oc_local._run, real = fake_run, oc_local._run
        try:
            with self.assertRaises(oc_local.ModelError):
                oc_local.complete("beam-free", "hi")
        finally:
            oc_local._run = real

    def test_infra_failure_is_local_upstream_error(self):
        import oc_local as m

        def boom(*args):
            raise m.LocalUpstreamError("nope")

        m._run, real = boom, m._run
        try:
            with self.assertRaises(m.LocalUpstreamError):
                m.complete("beam-free", "hi")
        finally:
            m._run = real


if __name__ == "__main__":
    unittest.main()
