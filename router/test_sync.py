#!/usr/bin/env python3
"""Tests for router/sync.py — OpenCode model-list sync into the picker.
Stdlib only. HTTP is exercised via data: URLs (real urlopen, no network).
"""
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sync


def _bind_ok():
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
    except OSError:
        return False
    return True


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


class FetchHeaderTests(unittest.TestCase):
    @unittest.skipUnless(_bind_ok(), "local port binding blocked in sandbox")
    def test_keyless_fetch_sends_plain_ua_no_auth(self):
        """The list endpoint rejects urllib's default UA; keyless fetch must
        look like a normal client and send no Authorization header."""
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["ua"] = self.headers.get("User-Agent")
                seen["auth"] = self.headers.get("Authorization")
                body = b'{"data": [{"id": "beam-free"}]}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            ids = sync.fetch_models("http://127.0.0.1:%d/models"
                                    % server.server_address[1])
        finally:
            server.shutdown()
        self.assertEqual(ids, ["beam-free"])
        self.assertIsNone(seen.get("auth"))
        self.assertNotIn("Python-urllib", seen.get("ua") or "")


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

    def test_no_key_still_lists(self):
        """Free-tier list is public: dry-run with no key stored succeeds."""
        saved = dict(os.environ)
        os.environ.pop("OPENCODE_API_KEY", None)
        try:
            url = models_url({"data": [{"id": "beam-free"}]})
            path = settings_file({})
            rc = sync.main(["--models-url", url, "--settings", path,
                            "--dry-run"])
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
