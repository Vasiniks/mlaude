#!/usr/bin/env python3
"""mlaude sync: register free OpenCode chat models as oc-<id> picker entries.

Stdlib only. Usage:
    python3 router/sync.py [--settings PATH] [--prune] [--dry-run]
                           [--models-url URL] [--key KEY]

Defaults: settings ~/.claude/settings.json, model list from
https://<OC_HOST><OC_MODELS_PATH> with the stored OpenCode key.
Writes atomically (temp + rename) with a .bak-mlaude-sync backup.
`jev-1.13-free` is skipped: Jev is a decision endpoint, not a chat model.
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.request
import urllib.error

try:
    import keys
    import connections
except ImportError:  # also runnable from inside router/
    from router import keys  # noqa: F401
    from router import connections  # noqa: F401

OC_HOST = os.environ.get("OC_HOST", "opencode.ai")
OC_MODELS_PATH = os.environ.get("OC_MODELS_PATH", "/inference/v1/models")
BACKUP_SUFFIX = ".bak-mlaude-sync"
# Decision endpoint, not a chat model: never routed, never registered.
JEV_EXCLUDE = {"jev-1.13-free"}


class SyncError(Exception):
    pass


def fetch_models(models_url, auth=None):
    """GET the model list, return [id, ...]. Raises SyncError."""
    headers = {"accept": "application/json",
               # The list endpoint refuses urllib's default UA.
               "User-Agent": "mlaude/2 (free-model sync)"}
    if auth:
        headers["authorization"] = auth
    req = urllib.request.Request(models_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SyncError("model list fetch failed: %s" % e)
    try:
        return [m["id"] for m in payload["data"]]
    except (KeyError, TypeError, AttributeError) as e:
        raise SyncError("unexpected model list shape: %s" % e)


def filter_free(ids):
    """Free chat models, minus the Jev decision endpoint."""
    return [i for i in ids
            if isinstance(i, str) and i.endswith("-free")
            and i not in JEV_EXCLUDE]


def entry_for(model_id):
    return {"model": "oc-" + model_id, "label": model_id,
            "description": "OpenCode, via local router"}


def apply_sync(model_ids, settings_path, prune=False):
    """Merge oc- entries into the picker. Returns diff
    {"added": [...], "updated": [...], "pruned": [...]} (model ids)."""
    wanted = {entry_for(i)["model"]: entry_for(i) for i in model_ids}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            try:
                cur = json.load(f)
            except ValueError as e:
                raise SyncError("%s is not valid JSON: %s"
                                % (settings_path, e))
        with open(settings_path, encoding="utf-8") as f:
            before = f.read()
    else:
        cur, before = {}, None
    picker = cur.setdefault("modelPicker", {})
    options = picker.setdefault("options", [])
    diff = {"added": [], "updated": [], "pruned": []}
    for i, o in enumerate(options):
        if isinstance(o, dict) and o.get("model") in wanted:
            fresh = wanted.pop(o["model"])
            if o != fresh:
                options[i] = fresh
                diff["updated"].append(fresh["model"])
    for model in sorted(wanted):
        options.append(wanted[model])
        diff["added"].append(model)
    if prune:
        keep = set(entry_for(i)["model"] for i in model_ids)
        kept_options = []
        for o in options:
            m = o.get("model") if isinstance(o, dict) else None
            if (isinstance(m, str) and m.startswith("oc-")
                    and m not in keep):
                diff["pruned"].append(m)
            else:
                kept_options.append(o)
        picker["options"] = kept_options
    if any(diff.values()):
        if before is not None:
            with open(settings_path + BACKUP_SUFFIX, "w",
                      encoding="utf-8") as f:
                f.write(before)
        parent = os.path.dirname(settings_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent or None, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=2)
            os.replace(tmp, settings_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return diff


def default_settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sync OpenCode models.")
    ap.add_argument("--settings", default=default_settings_path())
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--models-url", default=None)
    ap.add_argument("--key", default=None)
    args = ap.parse_args(argv)

    key = (args.key or "").strip() or keys.get_key("opencode")
    # Free-tier list is public: the key is optional, sent when present.
    auth = ("Bearer " + key) if key else None
    url = args.models_url
    if not url:
        cfg = connections.effective()
        url = "https://" + cfg["oc_host"] + cfg["oc_models_path"]
    try:
        ids = filter_free(fetch_models(url, auth))
    except SyncError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    if args.dry_run:
        print("would register %d models: %s"
              % (len(ids), ", ".join("oc-" + i for i in ids) or "(none)"))
        return 0
    try:
        diff = apply_sync(ids, args.settings, prune=args.prune)
    except SyncError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("sync: +%d ~%d -%d (%s)" % (
        len(diff["added"]), len(diff["updated"]), len(diff["pruned"]),
        ", ".join(diff["added"] + diff["updated"]) or "no changes"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
