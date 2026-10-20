#!/usr/bin/env python3
"""Endpoint settings managed by the dashboard (stdlib only).

File: ~/.config/mlaude/connections.json (override with
MLAUDE_CONNECTIONS_FILE). Only known keys with non-empty values are stored;
everything else falls back to defaults. Environment always wins, so
containers and one-off overrides keep working without a file.
"""
import json
import os

DEFAULTS = {
    "oc_host": "opencode.ai",
    "oc_path": "/zen/v1/chat/completions",
    "oc_models_path": "/inference/v1/models",
}

ENV = {
    "oc_host": "OC_HOST",
    "oc_path": "OC_PATH",
    "oc_models_path": "OC_MODELS_PATH",
}

DEFAULT_FILE = os.path.join(os.path.expanduser("~"),
                            ".config", "mlaude", "connections.json")


def _path(path=None):
    return path or os.environ.get("MLAUDE_CONNECTIONS_FILE") or DEFAULT_FILE


def load(path=None):
    """Stored overrides merged over defaults (unknown file keys ignored)."""
    cfg = dict(DEFAULTS)
    try:
        with open(_path(path), encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return cfg
    if isinstance(stored, dict):
        for k in DEFAULTS:
            v = stored.get(k)
            if isinstance(v, str) and v.strip():
                cfg[k] = v.strip()
    return cfg


def save(values, path=None):
    """Persist overrides, merged over what's already stored. Rejects
    unknown keys and empty values."""
    if not isinstance(values, dict):
        raise ValueError("connections must be an object")
    for k in values:
        if k not in DEFAULTS:
            raise ValueError("unknown connection: %r" % k)
    target = _path(path)
    try:
        with open(target, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    clean = {k: v for k, v in stored.items() if k in DEFAULTS}
    for k in DEFAULTS:
        if k in values:
            v = values[k]
            if not isinstance(v, str) or not v.strip():
                raise ValueError("empty value for %r" % k)
            clean[k] = v.strip()
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)


def effective(path=None):
    """Load with environment precedence (env > file > defaults)."""
    cfg = load(path)
    for k, env in ENV.items():
        v = os.environ.get(env, "").strip()
        if v:
            cfg[k] = v
    return cfg
