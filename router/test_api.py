#!/usr/bin/env python3
"""Tests for router/api.py — dashboard backend. Fake handler, temp files."""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api
import keys


class FakeHandler:
    def __init__(self, command="GET", path="/", body=None):
        self.command = command
        self.path = path
        raw = body if body is not None else b""
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw).encode()
        elif isinstance(raw, str):
            raw = raw.encode()
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = None
        self.resp_headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status, _reason=None):
        self.status = status

    def send_header(self, k, v):
        self.resp_headers[k.lower()] = v

    def end_headers(self):
        pass

    def body_json(self):
        return json.loads(self.wfile.getvalue().decode())


class Ctx:
    """Temp files plumbed into api.handle (no platform stores touched)."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="mlaude-api-")
        self.secrets = os.path.join(self.tmp, "secrets.json")
        self.conns = os.path.join(self.tmp, "connections.json")
        self.settings = os.path.join(self.tmp, "settings.json")
        self.ui = os.path.join(self.tmp, "ui.html")
        with open(self.ui, "w", encoding="utf-8") as f:
            f.write("<html>dash</html>")
        self.store = keys.FileStore(self.secrets)
        self.calls = []

    def handle(self, *args, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("settings_path", self.settings)
        kw.setdefault("ui_path", self.ui)
        kw.setdefault("connections_path", self.conns)
        kw.setdefault("log_paths", [os.path.join(self.tmp, "no.log")])
        return api.handle(*args, **kw)

    def on_restart(self):
        self.calls.append("restart")


class StatusTests(unittest.TestCase):
    def test_shape(self):
        ctx = Ctx()
        h = FakeHandler("GET", "/api/status")
        self.assertTrue(ctx.handle(h))
        self.assertEqual(h.status, 200)
        body = h.body_json()
        self.assertEqual(body["router"], "ok")
        self.assertIsInstance(body["port"], int)
        for svc in ("meta", "opencode", "anthropic"):
            self.assertIsInstance(body["keys"][svc], str)
        for k in ("muse", "oc", "other"):
            self.assertIsInstance(body["models"][k], int)


class KeysApiTests(unittest.TestCase):
    def test_set_then_get_masked(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/keys",
                        {"service": "opencode", "key": "oc-secret-value"})
        ctx.handle(h)
        self.assertEqual(h.status, 200)
        body = h.body_json()
        self.assertTrue(body["restart_required"])
        self.assertEqual(body["shown"], "oc-s…alue")
        h2 = FakeHandler("GET", "/api/keys")
        ctx.handle(h2)
        self.assertEqual(h2.body_json()["opencode"], "oc-s…alue")
        self.assertEqual(h2.body_json()["meta"], "missing")

    def test_secret_never_in_plaintext(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/keys",
                        {"service": "meta", "key": "meta-secret-value"})
        ctx.handle(h)
        raw = h.wfile.getvalue().decode()
        self.assertNotIn("meta-secret-value", raw)

    def test_bad_service_400(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/keys",
                        {"service": "nope", "key": "x"})
        ctx.handle(h)
        self.assertEqual(h.status, 400)

    def test_bad_json_400(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/keys", b"{oops")
        ctx.handle(h)
        self.assertEqual(h.status, 400)


class ConnectionsApiTests(unittest.TestCase):
    def test_round_trip(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/connections",
                        {"oc_host": "example.com"})
        ctx.handle(h)
        self.assertEqual(h.status, 200)
        h2 = FakeHandler("GET", "/api/connections")
        ctx.handle(h2)
        body = h2.body_json()
        self.assertEqual(body["oc_host"], "example.com")
        self.assertEqual(body["oc_path"], "/zen/v1/chat/completions")

    def test_unknown_key_400(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/connections", {"evil": "x"})
        ctx.handle(h)
        self.assertEqual(h.status, 400)


class ModelsLogTests(unittest.TestCase):
    def test_models_counts(self):
        ctx = Ctx()
        with open(ctx.settings, "w", encoding="utf-8") as f:
            json.dump({"modelPicker": {"options": [
                {"model": "muse-spark-1.3"},
                {"model": "oc-beam-free"},
                {"model": "claude-opus-4-6"}]}}, f)
        h = FakeHandler("GET", "/api/models")
        ctx.handle(h)
        body = h.body_json()
        self.assertEqual(body["counts"], {"muse": 1, "oc": 1, "other": 1})
        self.assertEqual(len(body["entries"]), 3)

    def test_log_missing_is_empty(self):
        ctx = Ctx()
        h = FakeHandler("GET", "/api/log")
        ctx.handle(h)
        self.assertEqual(h.body_json(), {"lines": []})

    def test_log_tails(self):
        ctx = Ctx()
        p = os.path.join(ctx.tmp, "r.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join("line%d" % i for i in range(10)) + "\n")
        h = FakeHandler("GET", "/api/log?lines=3")
        ctx.Calls = None
        api.handle(h, store=ctx.store, settings_path=ctx.settings,
                   ui_path=ctx.ui, connections_path=ctx.conns,
                   log_paths=[p])
        self.assertEqual(h.body_json(),
                         {"lines": ["line7", "line8", "line9"]})


class SyncRestartTests(unittest.TestCase):
    def test_sync_registers(self):
        import sync as sync_mod
        from urllib.parse import quote
        ctx = Ctx()
        ctx.store.set("opencode-api", "k")
        payload = quote(json.dumps({"data": [{"id": "beam-free"}]}))
        h = FakeHandler("POST", "/api/sync",
                        {"models_url": "data:application/json," + payload})
        ctx.handle(h)
        self.assertEqual(h.status, 200, h.wfile.getvalue().decode()[:200])
        body = h.body_json()
        self.assertEqual(body["diff"]["added"], ["oc-beam-free"])
        saved = json.load(open(ctx.settings, encoding="utf-8"))
        self.assertIn("oc-beam-free",
                      [o["model"] for o in saved["modelPicker"]["options"]])

    def test_sync_wrong_method_405(self):
        ctx = Ctx()
        h = FakeHandler("GET", "/api/sync")
        ctx.handle(h)
        self.assertEqual(h.status, 405)

    def test_sync_without_key_still_lists(self):
        """Free-tier list is public: no stored key needed."""
        import sync as sync_mod
        from urllib.parse import quote
        saved = dict(os.environ)
        os.environ.pop("OPENCODE_API_KEY", None)
        try:
            ctx = Ctx()  # empty FileStore: no opencode key stored
            payload = quote(json.dumps({"data": [{"id": "beam-free"}]}))
            h = FakeHandler("POST", "/api/sync",
                            {"models_url": "data:application/json," + payload})
            ctx.handle(h)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(h.status, 200, h.wfile.getvalue().decode()[:200])
        self.assertEqual(h.body_json()["diff"]["added"], ["oc-beam-free"])

    def test_restart_calls_hook(self):
        ctx = Ctx()
        h = FakeHandler("POST", "/api/restart")
        ctx.handle(h, on_restart=ctx.on_restart)
        self.assertEqual(h.status, 200)
        self.assertEqual(ctx.calls, ["restart"])


class SlotsApiTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        for v in ("META_API_KEY", "OPENCODE_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ.pop(v, None)
        self.tmp = tempfile.mkdtemp(prefix="mlaude-slots-api-")
        os.environ["MLAUDE_KEYSLOTS_FILE"] = os.path.join(
            self.tmp, "keyslots.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def post_slots(self, ctx, payload):
        h = FakeHandler("POST", "/api/slots", payload)
        ctx.handle(h)
        return h

    def test_add_activate_delete(self):
        ctx = Ctx()
        h = self.post_slots(ctx, {"action": "add", "name": "free2",
                                  "service": "opencode",
                                  "key": "slot-key-456"})
        self.assertEqual(h.status, 200, h.wfile.getvalue().decode()[:200])
        body = h.body_json()
        self.assertEqual(body["slots"]["slots"]["free2"]["shown"],
                         "slot…-456")
        self.assertTrue(body["restart_required"])
        h = self.post_slots(ctx, {"action": "activate",
                                  "service": "opencode", "name": "free2"})
        self.assertEqual(h.status, 200)
        h = FakeHandler("GET", "/api/keys")
        ctx.handle(h)
        body = h.body_json()
        self.assertEqual(body["slots"]["active"], {"opencode": "free2"})
        self.assertEqual(body["opencode"], "slot…-456")
        h = self.post_slots(ctx, {"action": "delete", "name": "free2"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body_json()["slots"]["slots"], {})
        self.assertEqual(h.body_json()["slots"]["active"], {})

    def test_bad_slot_requests_400(self):
        ctx = Ctx()
        for payload in ({"action": "frobnicate"},
                        {"action": "add", "name": "free2",
                         "service": "opencode"},
                        {"action": "activate", "service": "opencode",
                         "name": "nope"},
                        {"action": "delete"}):
            h = self.post_slots(ctx, payload)
            self.assertEqual(h.status, 400,
                             (payload, h.wfile.getvalue().decode()[:200]))

    def test_slots_wrong_method_405(self):
        ctx = Ctx()
        h = FakeHandler("GET", "/api/slots")
        ctx.handle(h)
        self.assertEqual(h.status, 405)

    def test_unknown_api_404(self):
        ctx = Ctx()
        h = FakeHandler("GET", "/api/nope")
        ctx.handle(h)
        self.assertEqual(h.status, 404)

    def test_ui_served(self):
        ctx = Ctx()
        h = FakeHandler("GET", "/ui")
        ctx.handle(h)
        self.assertEqual(h.status, 200)
        self.assertIn("text/html", h.resp_headers["content-type"])
        self.assertIn("dash", h.wfile.getvalue().decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
