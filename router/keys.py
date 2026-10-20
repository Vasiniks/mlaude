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
import re
import subprocess
import sys
import tempfile

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
    """Return the secret for `service`, or None.

    Precedence: environment, then the active custom slot (if any),
    then the primary platform store.
    """
    svc = _service(service)
    env_value = os.environ.get(svc["env"], "").strip()
    if env_value:
        return env_value
    slot_key = active_slot_key(service)
    if slot_key:
        return slot_key
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


# ---------------------------------------------------------- custom slots
# Named extra keys ("free2", "work-paid", ...) beyond the three primary
# services, added through the dashboard. One slot per service may be
# active; the router then uses it instead of the primary until restart.
# Registry + secrets live in one 600 file (override: MLAUDE_KEYSLOTS_FILE).

SLOT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

DEFAULT_KEYSLOTS_FILE = os.path.join(os.path.expanduser("~"),
                                     ".config", "mlaude", "keyslots.json")


def _slots_path(path=None):
    return path or os.environ.get("MLAUDE_KEYSLOTS_FILE") \
        or DEFAULT_KEYSLOTS_FILE


def _read_slots(path=None):
    try:
        with open(_slots_path(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"slots": {}, "active": {}}
    if not isinstance(data, dict):
        return {"slots": {}, "active": {}}
    slots = data.get("slots")
    active = data.get("active")
    return {"slots": slots if isinstance(slots, dict) else {},
            "active": active if isinstance(active, dict) else {}}


def _write_slots(state, path=None):
    target = _slots_path(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or None, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)


def _check_slot_name(name):
    if not isinstance(name, str) or not SLOT_NAME.match(name) \
            or name in SERVICES:
        raise ValueError("bad slot name %r (a-z, 0-9, dash; "
                         "not a service name)" % (name,))


def add_slot(name, service, key, slots_path=None):
    """Register a named extra key for `service`."""
    _check_slot_name(name)
    _service(service)
    key = key.strip() if isinstance(key, str) else ""
    if not key:
        raise ValueError("empty key for slot %r; aborting" % name)
    state = _read_slots(slots_path)
    state["slots"][name] = {"service": service, "key": key}
    _write_slots(state, slots_path)


def delete_slot(name, slots_path=None):
    """Remove a slot (active refs to it fall back to primary)."""
    state = _read_slots(slots_path)
    if name not in state["slots"]:
        raise ValueError("unknown slot: %r" % (name,))
    del state["slots"][name]
    state["active"] = {s: n for s, n in state["active"].items()
                       if n != name}
    _write_slots(state, slots_path)


def set_active(service, name, slots_path=None):
    """Make `name` the live key for `service` (None/"" = primary)."""
    _service(service)
    state = _read_slots(slots_path)
    if name is None or (isinstance(name, str) and not name.strip()):
        state["active"].pop(service, None)
    else:
        slot = state["slots"].get(name)
        if slot is None:
            raise ValueError("unknown slot: %r" % (name,))
        if slot.get("service") != service:
            raise ValueError("slot %r serves %r, not %r"
                             % (name, slot.get("service"), service))
        state["active"][service] = name
    _write_slots(state, slots_path)


def active_slot_key(service, slots_path=None):
    """Key from the active custom slot, or None (primary applies)."""
    _service(service)
    state = _read_slots(slots_path)
    name = state["active"].get(service)
    if not name:
        return None
    slot = state["slots"].get(name)
    if not isinstance(slot, dict) or slot.get("service") != service:
        return None
    key = slot.get("key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def list_slots(slots_path=None):
    """Masked slot inventory (never plaintext) + active map."""
    state = _read_slots(slots_path)
    out = {}
    for name in sorted(state["slots"]):
        slot = state["slots"][name]
        if not isinstance(slot, dict):
            continue
        out[name] = {"service": slot.get("service"),
                     "shown": mask(slot.get("key"))}
    active = {s: n for s, n in state["active"].items()
              if n in state["slots"]}
    return {"slots": out, "active": active}


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
        found = list_slots()
        for name, slot in found["slots"].items():
            mark = " [active]" if found["active"].get(
                slot["service"]) == name else ""
            print("slot %s (%s): present (%s)%s"
                  % (name, slot["service"], slot["shown"], mark))
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
