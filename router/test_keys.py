#!/usr/bin/env python3
"""Tests for router/keys.py — platform key storage. Stdlib only.

Real FileStore on temp paths, real environ manipulation (saved/restored),
real `security` read of a missing item on macOS. No network.
"""
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keys


class MaskTests(unittest.TestCase):
    def test_long_key_shows_first4_last4(self):
        self.assertEqual(keys.mask("sk-ant-1234567890"), "sk-a…7890")

    def test_missing_key(self):
        self.assertEqual(keys.mask(None), "missing")
        self.assertEqual(keys.mask(""), "missing")

    def test_short_key_hidden(self):
        self.assertEqual(keys.mask("abc123"), "••••")


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlaude-keys-")
        self.path = os.path.join(self.tmp, "sub", "secrets.json")
        self.store = keys.FileStore(self.path)

    def test_missing_service_returns_none(self):
        self.assertIsNone(self.store.get("meta-model-api"))

    def test_set_then_get_round_trip(self):
        self.store.set("opencode-api", "oc-key-1")
        self.assertEqual(self.store.get("opencode-api"), "oc-key-1")

    def test_set_creates_parents_and_locks_down_file(self):
        self.store.set("meta-model-api", "m1")
        self.assertTrue(os.path.isfile(self.path))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        raw = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(raw["meta-model-api"], "m1")

    def test_set_overwrites_and_keeps_others(self):
        self.store.set("a", "1")
        self.store.set("b", "2")
        self.store.set("a", "3")
        self.assertEqual(self.store.get("a"), "3")
        self.assertEqual(self.store.get("b"), "2")


class GetSetKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlaude-keys-")
        self.path = os.path.join(self.tmp, "secrets.json")
        self.saved = dict(os.environ)
        os.environ["MLAUDE_SECRETS_FILE"] = self.path
        os.environ.pop("OPENCODE_API_KEY", None)
        os.environ.pop("META_API_KEY", None)
        self.store = keys.FileStore()  # honors MLAUDE_SECRETS_FILE

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_unknown_service_rejected(self):
        with self.assertRaises(ValueError):
            keys.get_key("nope")

    def test_env_beats_store(self):
        keys.set_key("opencode", "from-store", store=self.store)
        os.environ["OPENCODE_API_KEY"] = "from-env"
        self.assertEqual(keys.get_key("opencode", store=self.store), "from-env")

    def test_falls_through_to_store(self):
        keys.set_key("meta", "store-key", store=self.store)
        self.assertEqual(keys.get_key("meta", store=self.store), "store-key")

    def test_absent_everywhere_is_none(self):
        self.assertIsNone(keys.get_key("anthropic", store=self.store))

    def test_set_strips_whitespace(self):
        keys.set_key("meta", "  spaced-key\n", store=self.store)
        self.assertEqual(keys.get_key("meta", store=self.store), "spaced-key")

    def test_set_empty_rejected(self):
        with self.assertRaises(ValueError):
            keys.set_key("meta", "   ", store=self.store)

    def test_key_present(self):
        self.assertFalse(keys.key_present("anthropic", store=self.store))
        keys.set_key("anthropic", "x" * 12, store=self.store)
        self.assertTrue(keys.key_present("anthropic", store=self.store))


class PlatformSelectionTests(unittest.TestCase):
    def test_this_machine_selects_matching_store(self):
        store = keys._platform_store()
        if sys.platform == "darwin":
            self.assertIsInstance(store, keys.MacStore)
        elif sys.platform == "win32":
            self.assertIsInstance(store, keys.WindowsStore)
        else:
            self.assertIsInstance(store, keys.FileStore)

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain only")
    def test_mac_missing_item_returns_none(self):
        store = keys.MacStore()
        self.assertIsNone(store.get("mlaude-test-no-such-service"))


class KeysCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlaude-keys-cli-")
        self.store = os.path.join(self.tmp, "s.json")
        self.keys_py = os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "keys.py")

    def run_cli(self, *args, **kw):
        import subprocess
        return subprocess.run(
            [sys.executable, self.keys_py, "--store-file", self.store]
            + list(args),
            capture_output=True, text=True, timeout=30, **kw)

    def test_list_empty_store(self):
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        for svc in ("meta", "opencode", "anthropic"):
            self.assertIn("%s: missing" % svc, r.stdout)

    def test_set_then_list_masked(self):
        r = self.run_cli("set", "opencode", input="oc-test-key\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("list")
        self.assertIn("opencode: present (oc-t…-key)", r.stdout)
        self.assertNotIn("oc-test-key", r.stdout)

    def test_set_unknown_service_fails(self):
        r = self.run_cli("set", "nope", input="x\n")
        self.assertNotEqual(r.returncode, 0)

    def test_set_empty_fails(self):
        r = self.run_cli("set", "meta", input="   \n")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
