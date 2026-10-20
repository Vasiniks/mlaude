#!/usr/bin/env python3
"""Tests for router/connections.py — dashboard-managed endpoint settings."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import connections


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        os.environ.pop("OC_HOST", None)
        os.environ.pop("OC_PATH", None)
        os.environ.pop("OC_MODELS_PATH", None)
        self.tmp = tempfile.mkdtemp(prefix="mlaude-conn-")
        self.path = os.path.join(self.tmp, "connections.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_defaults_when_no_file(self):
        cfg = connections.load(self.path)
        self.assertEqual(cfg["oc_host"], "opencode.ai")
        self.assertEqual(cfg["oc_path"], "/zen/v1/chat/completions")
        self.assertEqual(cfg["oc_models_path"], "/inference/v1/models")

    def test_save_load_round_trip(self):
        connections.save({"oc_host": "example.com"}, self.path)
        cfg = connections.load(self.path)
        self.assertEqual(cfg["oc_host"], "example.com")
        # Untouched keys keep defaults.
        self.assertEqual(cfg["oc_path"], "/zen/v1/chat/completions")

    def test_save_merges_with_stored_siblings(self):
        connections.save({"oc_host": "example.com"}, self.path)
        connections.save({"oc_path": "/custom"}, self.path)
        cfg = connections.load(self.path)
        self.assertEqual(cfg["oc_host"], "example.com")
        self.assertEqual(cfg["oc_path"], "/custom")

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            connections.save({"evil": "x"}, self.path)

    def test_empty_value_rejected(self):
        with self.assertRaises(ValueError):
            connections.save({"oc_host": "  "}, self.path)

    def test_env_beats_file(self):
        connections.save({"oc_host": "file.example"}, self.path)
        os.environ["OC_HOST"] = "env.example"
        cfg = connections.effective(self.path)
        self.assertEqual(cfg["oc_host"], "env.example")
        self.assertEqual(cfg["oc_path"], "/zen/v1/chat/completions")


if __name__ == "__main__":
    unittest.main(verbosity=2)
