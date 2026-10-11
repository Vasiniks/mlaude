#!/usr/bin/env python3
"""mlaude add-model: register a model in Claude Code's model picker.

Works on macOS, Linux, and Windows (stdlib only, like the router).

Examples:
  # add Muse + make it active
  python3 add-model.py --model muse-spark-1.3-contributor \
      --label "Muse Spark 1.3 (contributor)" \
      --description "Meta, 1M context, via local router" --use

  # add a model without switching to it
  python3 add-model.py --model claude-opus-4-6 --label "Opus 4.6"

  # show the active model and every picker entry
  python3 add-model.py --list

Restart Claude Code after adding for the picker to refresh.
"""
import argparse
import json
import os
import sys

BACKUP_SUFFIX = ".bak-mlaude-add-model"


def settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def load(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def cmd_list(cur):
    print("active model: %s" % cur.get("model", "(none)"))
    options = (cur.get("modelPicker") or {}).get("options") or []
    if not options:
        print("picker entries: (none)")
        return
    print("picker entries:")
    for o in options:
        mark = "*" if o.get("model") == cur.get("model") else " "
        print("  %s %s — %s" % (mark, o.get("model"), o.get("label") or ""))


def main():
    ap = argparse.ArgumentParser(description="Register a model in Claude Code's picker.")
    ap.add_argument("--model", help="model id, e.g. muse-spark-1.3-contributor")
    ap.add_argument("--label", help="picker label (defaults to the model id)")
    ap.add_argument("--description", default=None, help="picker description (kept as-is on update if omitted)")
    ap.add_argument("--use", action="store_true", help="also set it as the active model")
    ap.add_argument("--list", action="store_true", help="show active model and picker entries")
    args = ap.parse_args()

    path = settings_path()
    cur = load(path)

    if args.list:
        cmd_list(cur)
        return 0

    if not args.model:
        ap.error("--model is required (or use --list)")

    entry = {"model": args.model, "label": args.label or args.model,
             "description": args.description}
    picker = cur.setdefault("modelPicker", {})
    options = picker.setdefault("options", [])
    for i, o in enumerate(options):
        if isinstance(o, dict) and o.get("model") == args.model:
            if args.label is None:
                entry["label"] = o.get("label") or entry["label"]
            if args.description is None:
                entry["description"] = o.get("description") or ""
            options[i] = entry
            action = "updated"
            break
    else:
        if args.description is None:
            entry["description"] = ""
        options.append(entry)
        action = "added"

    if args.use:
        cur["model"] = args.model

    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            before = f.read()
        after = json.dumps(cur, indent=2)
        if before.strip() == after.strip():
            print("no change needed")
            return 0
        with open(path + BACKUP_SUFFIX, "w", encoding="utf-8") as f:
            f.write(before)
        print("backed up to %s" % path + BACKUP_SUFFIX)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2)
    print('%s "%s" in the picker%s' % (action, entry["label"], " and set active" if args.use else ""))
    print("restart Claude Code to see it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
