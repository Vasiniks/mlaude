#!/usr/bin/env python3
"""Tests for router/sync.py — OpenCode model-list sync into the picker.
Stdlib only. HTTP is exercised via data: URLs (real urlopen, no network).
"""
import json
import os
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sync


def settings_file(initial=None):
    tmp = tempfile.mkdtemp(prefix="mlaude-sync-")
    path = os.path.join(tmp, "settings.json")
    if initial is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(initial, f)
    return path


def models_url(payload):
    return "data:application/json," + quote(json.dumps(payload))


class FilterTests(unittest.TestCase):
    def test_keeps_free_drops_rest_and_jev(self):
        ids = ["beam-free", "beam-pro", "jev-1.13-free", "pulse-free"]
        self.assertEqual(sync.filter_free(ids), ["beam-free", "pulse-free"])


class ApplyTests(unittest.TestCase):
    def test_adds_to_empty_settings(self):
        path = settings_file({})
        diff = sync.apply_sync(["beam-free"], path)
        self.assertEqual(diff["added"], ["oc-beam-free"])
        self.assertEqual(diff["pruned"], [])
        cur = json.load(open(path, encoding="utf-8"))
        models = [o["model"] for o in cur["modelPicker"]["options"]]
        self.assertIn("oc-beam-free", models)

    def test_second_run_is_noop_and_writes_nothing(self):
        path = settings_file({})
        sync.apply_sync(["beam-free"], path)
        before = open(path, encoding="utf-8").read()
        diff = sync.apply_sync(["beam-free"], path)
        self.assertEqual(diff, {"added": [], "updated": [], "pruned": []})
        self.assertEqual(open(path, encoding="utf-8").read(), before)

    def test_backup_holds_original(self):
        initial = {"model": "old"}
        path = settings_file(initial)
        sync.apply_sync(["beam-free"], path)
        bak = json.load(open(path + ".bak-mlaude-sync", encoding="utf-8"))
        self.assertEqual(bak, initial)

    def test_prune_removes_only_stale_oc_entries(self):
        initial = {"modelPicker": {"options": [
            {"model": "oc-gone-free", "label": "x"},
            {"model": "oc-beam-free", "label": "beam-free"},
            {"model": "muse-spark-1.3", "label": "Muse"},
            {"model": "claude-opus-4-6", "label": "Opus"}]}}
        path = settings_file(initial)
        diff = sync.apply_sync(["beam-free"], path, prune=True)
        self.assertEqual(diff["pruned"], ["oc-gone-free"])
        models = [o["model"] for o in
                  json.load(open(path, encoding="utf-8"))["modelPicker"]["options"]]
        self.assertNotIn("oc-gone-free", models)
        self.assertIn("muse-spark-1.3", models)
        self.assertIn("claude-opus-4-6", models)

    def test_no_prune_by_default(self):
        initial = {"modelPicker": {"options": [
            {"model": "oc-gone-free", "label": "x"}]}}
        path = settings_file(initial)
        diff = sync.apply_sync(["beam-free"], path, prune=False)
        self.assertEqual(diff["pruned"], [])
        models = [o["model"] for o in
                  json.load(open(path, encoding="utf-8"))["modelPicker"]["options"]]
        self.assertIn("oc-gone-free", models)


class FetchTests(unittest.TestCase):
    def test_data_url(self):
        url = models_url({"data": [{"id": "beam-free"}, {"id": "beam-pro"}]})
        self.assertEqual(sync.fetch_models(url, "Bearer test"),
                         ["beam-free", "beam-pro"])

    def test_bad_payload_raises(self):
        url = models_url({"nope": []})
        with self.assertRaises(sync.SyncError):
            sync.fetch_models(url, "Bearer test")


class MainTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        url = models_url({"data": [{"id": "beam-free"}]})
        path = settings_file({})
        before = open(path, encoding="utf-8").read()
        rc = sync.main(["--models-url", url, "--settings", path,
                        "--key", "test", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(open(path, encoding="utf-8").read(), before)

    def test_unreachable_host_exits_1(self):
        path = settings_file({})
        rc = sync.main(["--models-url", "http://127.0.0.1:9/models",
                        "--settings", path, "--key", "test"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
