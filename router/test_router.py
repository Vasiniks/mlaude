#!/usr/bin/env python3
"""Tests for Task 3 router wiring: route selection, local-path interception,
oc key loading, Anthropic override injection. Stdlib only, no network.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keys
import router


class RouteForTests(unittest.TestCase):
    def test_muse_goes_meta(self):
        self.assertEqual(router.route_for("muse-spark-1.3"), "meta")

    def test_oc_goes_opencode(self):
        self.assertEqual(router.route_for("oc-beam-free"), "opencode")

    def test_anything_else_goes_anthropic(self):
        for m in ("claude-opus-4-6", "", "gpt-4", "Muse-spark"):
            self.assertEqual(router.route_for(m), "anthropic", m)


class LocalPathTests(unittest.TestCase):
    def test_dashboard_paths_intercepted(self):
        for p in ("/ui", "/ui/", "/api", "/api/status", "/api/keys"):
            self.assertTrue(router.is_local_path(p), p)

    def test_proxy_paths_pass_through(self):
        for p in ("/", "/v1/messages", "/apiary", "/uin", "/v1/models"):
            self.assertFalse(router.is_local_path(p), p)


class KeyWiringTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        os.environ.pop("OPENCODE_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.tmp = tempfile.mkdtemp(prefix="mlaude-router-")
        self.store = keys.FileStore(os.path.join(self.tmp, "s.json"))
        router._oc_loaded = False
        router._oc_key = None
        router._ao_loaded = False
        router._ao_key = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        router._oc_loaded = False
        router._oc_key = None
        router._ao_loaded = False
        router._ao_key = None

    def test_oc_key_from_store(self):
        self.store.set("opencode-api", "oc-secret")
        self.assertEqual(router.oc_key(store=self.store), "oc-secret")

    def test_oc_key_missing_is_none(self):
        self.assertIsNone(router.oc_key(store=self.store))

    def test_oc_key_cached_restart_required(self):
        self.store.set("opencode-api", "first")
        self.assertEqual(router.oc_key(store=self.store), "first")
        self.store.set("opencode-api", "second")
        self.assertEqual(router.oc_key(store=self.store), "first")

    def test_override_absent_by_default(self):
        self.assertIsNone(router.anthropic_override(store=self.store))

    def test_override_returned_when_stored(self):
        self.store.set("anthropic-api", "sk-ant-x")
        self.assertEqual(router.anthropic_override(store=self.store),
                         "sk-ant-x")


class OverrideHeaderTests(unittest.TestCase):
    def test_override_replaces_client_key(self):
        headers = {"x-api-key": "client-login", "other": "kept"}
        router.apply_anthropic_override(headers, "sk-ant-override")
        self.assertEqual(headers["x-api-key"], "sk-ant-override")
        self.assertEqual(headers["other"], "kept")

    def test_no_override_leaves_headers_alone(self):
        headers = {"x-api-key": "client-login"}
        router.apply_anthropic_override(headers, None)
        self.assertEqual(headers, {"x-api-key": "client-login"})


class UnsupportedErrorTests(unittest.TestCase):
    def test_shape(self):
        status, payload = router.unsupported_error("oc-beam-free", "role X")
        self.assertEqual(status, 400)
        body = json.loads(payload.decode())
        self.assertEqual(body["type"], "error")
        self.assertIn("oc-beam-free", body["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
