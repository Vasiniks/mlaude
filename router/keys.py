#!/usr/bin/env python3
"""Platform key storage for mlaude (stdlib only).

Services: "meta", "opencode", "anthropic" (optional override).
Reads prefer the process environment (containers, one-off overrides);
writes go to the platform store: macOS Keychain, Windows User env
(registry), or ~/.config/mlaude/secrets.json (mode 600) elsewhere.
`MLAUDE_SECRETS_FILE` overrides the file path (tests, containers).
"""
import getpass
import json
import os
import subprocess
import sys

SERVICES = {
    "meta": {"keychain": "meta-model-api",
             "env": "META_API_KEY", "label": "Meta"},
    "opencode": {"keychain": "opencode-api",
                 "env": "OPENCODE_API_KEY", "label": "OpenCode"},
    "anthropic": {"keychain": "anthropic-api",
                  "env": "ANTHROPIC_API_KEY",
                  "label": "Anthropic (override)"},
}

DEFAULT_SECRETS_FILE = os.path.join(os.path.expanduser("~"),
                                    ".config", "mlaude", "secrets.json")


def _service(name):
    try:
        return SERVICES[name]
    except KeyError:
        raise ValueError("unknown key service: %r (one of %s)"
                         % (name, ", ".join(sorted(SERVICES))))


def mask(value):
    """Short masked form for status displays; never the secret itself."""
    if not value:
        return "missing"
    if len(value) <= 8:
        return "••••"
    return value[:4] + "…" + value[-4:]


class FileStore:
    """JSON file store (Linux default, fallback everywhere)."""

    def __init__(self, path=None):
        self.path = (path or os.environ.get("MLAUDE_SECRETS_FILE")
                     or DEFAULT_SECRETS_FILE)

    def _read_all(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, keychain_name):
        return self._read_all().get(keychain_name)

    def set(self, keychain_name, value):
        data = self._read_all()
        data[keychain_name] = value
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(self.path, 0o600)


class MacStore:
    """macOS Keychain via `security` (no dependencies, no prompts on read)."""

    def get(self, keychain_name):
        try:
            out = subprocess.run(
                ["security", "find-generic-password",
                 "-a", getpass.getuser(), "-s", keychain_name, "-w"],
                capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, OSError):
            return None
        return out.stdout.strip() or None

    def set(self, keychain_name, value):
        subprocess.run(
            ["security", "add-generic-password", "-U",
             "-a", getpass.getuser(), "-s", keychain_name, "-w", value],
            capture_output=True, text=True, check=True)


class WindowsStore:
    """Windows User environment (registry HKCU\\Environment)."""

    def _reg(self):
        import winreg
        return winreg

    def get(self, keychain_name):
        svc = _service_for_keychain(keychain_name)
        winreg = self._reg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value, _ = winreg.QueryValueEx(k, svc["env"])
        except (OSError, ImportError):
            return os.environ.get(svc["env"]) or None
        return value.strip() or None

    def set(self, keychain_name, value):
        svc = _service_for_keychain(keychain_name)
        winreg = self._reg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, svc["env"], 0, winreg.REG_EXPAND_SZ, value)
        os.environ[svc["env"]] = value


def _service_for_keychain(keychain_name):
    for svc in SERVICES.values():
        if svc["keychain"] == keychain_name:
            return svc
    raise ValueError("unknown keychain service: %r" % keychain_name)


def _platform_store():
    if sys.platform == "darwin":
        return MacStore()
    if sys.platform == "win32":
        return WindowsStore()
    return FileStore()


def get_key(service, store=None):
    """Return the secret for `service`, or None. Env var wins over the store."""
    svc = _service(service)
    env_value = os.environ.get(svc["env"], "").strip()
    if env_value:
        return env_value
    store = store if store is not None else _platform_store()
    value = store.get(svc["keychain"])
    return value.strip() if isinstance(value, str) and value.strip() else None


def set_key(service, value, store=None):
    """Persist `value` for `service`. Surrounding whitespace is stripped."""
    svc = _service(service)
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise ValueError("empty key for service %r; aborting" % service)
    store = store if store is not None else _platform_store()
    store.set(svc["keychain"], value)


def key_present(service, store=None):
    """True when a non-empty secret resolves (env or store)."""
    return get_key(service, store=store) is not None


def _resolve_store(store_file):
    if store_file:
        return FileStore(store_file)
    return _platform_store()


def main(argv=None):
    import argparse
    import getpass as _getpass
    ap = argparse.ArgumentParser(description="Manage mlaude API keys.")
    ap.add_argument("--store-file", default=None,
                    help="use this JSON file instead of the platform store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show which keys are present (masked)")
    p_set = sub.add_parser("set", help="store a key (prompts, never echoed)")
    p_set.add_argument("service",
                       help="one of: %s" % ", ".join(sorted(SERVICES)))
    args = ap.parse_args(argv)
    store = _resolve_store(args.store_file)
    if args.cmd == "list":
        for name in sorted(SERVICES):
            shown = mask(get_key(name, store=store))
            if shown == "missing":
                print("%s: missing" % name)
            else:
                print("%s: present (%s)" % (name, shown))
        return 0
    if args.cmd == "set":
        try:
            svc = _service(args.service)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        try:
            value = _getpass.getpass("%s key: " % svc["label"])
        except (EOFError, KeyboardInterrupt):
            print("")
            return 1
        try:
            set_key(args.service, value, store=store)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        print("stored %s key" % args.service)
        return 0
    ap.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
