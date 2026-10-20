"""Dashboard backend for mlaude (stdlib only, no network calls of its own).

handle() serves GET /ui plus the /api/* routes the dashboard uses.
All file locations are overridable parameters so tests use temp files;
production defaults point at the real Keychain/settings/log paths.
"""
import json
import os
import sys
import urllib.parse

import connections
import keys
import sync

MAX_BODY = 65536
MAX_LOG_BYTES = 262144

UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude",
                             "settings.json")


def _json(handler, status, obj):
    payload = json.dumps(obj, ensure_ascii=False,
                         separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(payload)


def _err(handler, status, message, etype="invalid_request_error"):
    _json(handler, status, {"type": "error",
                            "error": {"type": etype, "message": message}})


def _read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length > MAX_BODY:
        return None, "body too large"
    raw = handler.rfile.read(length) if length else b""
    if not raw.strip():
        return {}, None
    try:
        body = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return None, "invalid JSON"
    if not isinstance(body, dict):
        return None, "body must be an object"
    return body, None


def _settings_entries(settings_path):
    try:
        with open(settings_path or SETTINGS_PATH, encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        return []
    options = (cur.get("modelPicker") or {}).get("options") or []
    return [o for o in options if isinstance(o, dict)]


def _counts(entries):
    counts = {"muse": 0, "oc": 0, "other": 0}
    for o in entries:
        m = o.get("model") or ""
        if m.startswith("muse-"):
            counts["muse"] += 1
        elif m.startswith("oc-"):
            counts["oc"] += 1
        else:
            counts["other"] += 1
    return counts


def _default_log_paths():
    home = os.path.expanduser("~")
    router_dir = os.path.join(home, ".local", "share", "claude-router")
    return [os.path.join(home, "Library", "Logs", "claude-router.log"),
            os.path.join(router_dir, "router.log")]


def _tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - MAX_LOG_BYTES))
            data = f.read().decode(errors="replace")
    except OSError:
        return None
    return data.splitlines()[-n:]


def _sync_state(settings_path):
    """Last sync time, derived from the sync backup (None = never)."""
    try:
        st = os.stat((settings_path or SETTINGS_PATH)
                     + sync.BACKUP_SUFFIX)
    except OSError:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(
        st.st_mtime).isoformat(timespec="seconds")


def _do_restart():
    here = os.path.dirname(os.path.abspath(__file__))
    os.execv(sys.executable,
             [sys.executable, os.path.join(here, "router.py")])


def handle(handler, store=None, settings_path=None, ui_path=None,
           connections_path=None, log_paths=None, on_restart=None):
    """Serve one local /ui or /api/* request. Always returns True."""
    if log_paths is None:
        log_paths = _default_log_paths()
    bare = handler.path.split("?", 1)[0]
    query = urllib.parse.parse_qs(handler.path.split("?", 1)[1]
                                  if "?" in handler.path else "")

    if bare == "/ui" or bare.startswith("/ui/"):
        if handler.command not in ("GET", "HEAD"):
            _err(handler, 405, "use GET")
            return True
        try:
            with open(ui_path or UI_PATH, "rb") as f:
                page = f.read()
        except OSError:
            _err(handler, 500, "dashboard not installed",
                 etype="api_error")
            return True
        handler.send_response(200)
        handler.send_header("content-type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(page)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(page)
        return True

    if bare == "/api/status" and handler.command == "GET":
        entries = _settings_entries(settings_path)
        _json(handler, 200, {
            "router": "ok",
            "port": int(os.environ.get("ROUTER_PORT", "8787")),
            "keys": {s: keys.mask(keys.get_key(s, store=store))
                     for s in ("meta", "opencode", "anthropic")},
            "models": _counts(entries),
            "connections": connections.effective(connections_path),
            "last_sync": _sync_state(settings_path)})
        return True

    if bare == "/api/keys" and handler.command == "GET":
        _json(handler, 200,
              {s: keys.mask(keys.get_key(s, store=store))
               for s in ("meta", "opencode", "anthropic")})
        return True

    if bare == "/api/keys" and handler.command == "POST":
        body, err = _read_json(handler)
        if err:
            _err(handler, 400, err)
            return True
        service, secret = body.get("service"), body.get("key")
        if not service or not isinstance(secret, str) or not secret.strip():
            _err(handler, 400, "need {service, key}")
            return True
        try:
            keys.set_key(service, secret, store=store)
        except ValueError as e:
            _err(handler, 400, str(e))
            return True
        _json(handler, 200, {"service": service,
                             "shown": keys.mask(secret.strip()),
                             "restart_required": True})
        return True

    if bare == "/api/connections" and handler.command == "GET":
        _json(handler, 200, connections.effective(connections_path))
        return True

    if bare == "/api/connections" and handler.command == "POST":
        body, err = _read_json(handler)
        if err:
            _err(handler, 400, err)
            return True
        try:
            connections.save(body, connections_path)
        except ValueError as e:
            _err(handler, 400, str(e))
            return True
        _json(handler, 200, {"connections":
                             connections.effective(connections_path),
                             "restart_required": False})
        return True

    if bare == "/api/models" and handler.command == "GET":
        entries = _settings_entries(settings_path)
        _json(handler, 200, {"entries": entries,
                             "counts": _counts(entries)})
        return True

    if bare == "/api/log" and handler.command == "GET":
        try:
            n = min(500, max(1, int((query.get("lines") or ["50"])[0])))
        except ValueError:
            n = 50
        lines = []
        for p in log_paths:
            lines = _tail(p, n)
            if lines is not None:
                break
        _json(handler, 200, {"lines": lines or []})
        return True

    if bare == "/api/sync" and handler.command == "POST":
        body, err = _read_json(handler)
        if err:
            _err(handler, 400, err)
            return True
        key = keys.get_key("opencode", store=store)
        # Free-tier list is public: the key is optional, sent when present.
        auth = ("Bearer " + key) if key else None
        cfg = connections.effective(connections_path)
        url = body.get("models_url") or ("https://" + cfg["oc_host"]
                                         + cfg["oc_models_path"])
        try:
            ids = sync.filter_free(sync.fetch_models(url, auth))
            diff = sync.apply_sync(ids, settings_path or SETTINGS_PATH,
                                   prune=bool(body.get("prune")))
        except sync.SyncError as e:
            _err(handler, 502, str(e))
            return True
        _json(handler, 200, {"diff": diff})
        return True

    if bare == "/api/restart" and handler.command == "POST":
        _json(handler, 200, {"restarting": True})
        try:
            handler.wfile.flush()
        except (OSError, ValueError):
            pass
        (on_restart or _do_restart)()
        return True

    if bare.startswith("/api/"):
        if bare in ("/api/status", "/api/models", "/api/log",
                    "/api/sync", "/api/restart"):
            _err(handler, 405, "method not allowed")
        elif handler.command in ("GET", "HEAD", "POST"):
            _err(handler, 404, "unknown API route", etype="not_found_error")
        else:
            _err(handler, 405, "method not allowed")
        return True

    _err(handler, 404, "unknown route", etype="not_found_error")
    return True
