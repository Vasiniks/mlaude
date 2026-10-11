#!/usr/bin/env python3
"""Local Claude Code router.

Requests whose "model" starts with "muse-" go to Meta (through its Responses
API via muse_responses.py, or its Anthropic-compatible API as a fallback);
everything else goes to api.anthropic.com with your normal Claude login,
forwarded byte-for-byte and streamed through.
"""
import getpass
import http.client
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import muse_responses

PORT = int(os.environ.get("ROUTER_PORT", "8787"))
# "responses" (default) or "messages": which Meta API serves Muse requests.
MUSE_API = os.environ.get("ROUTER_MUSE_API", "responses")
ANTHROPIC_HOST = "api.anthropic.com"
META_HOST = "api.meta.ai"
MUSE_PREFIX = "muse-"
KEYCHAIN_SERVICE = "meta-model-api"

# Hop-by-hop or re-computed headers; never forwarded in either direction.
SKIP = {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding",
        "te", "trailer", "upgrade", "content-length", "accept-encoding", "date", "server"}

_meta_key = None

# Meta's schema validator rejects the regex escape \0 (NUL) but accepts the
# equivalent \x00. Matches a \0 that is a real escape (odd run of backslashes).
NUL_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\0(?![0-9])")
# Schema keywords whose values are data, not sub-schemas.
DATA_KEYWORDS = {"enum", "const", "default", "examples"}


def fix_schema(node):
    """Rewrite regex patterns Meta rejects into equivalent ones. Returns True if changed."""
    changed = False
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "pattern" and isinstance(v, str):
                new = NUL_ESCAPE.sub(r"\1\\x00", v)
                if new != v:
                    node[k] = new
                    changed = True
            elif k not in DATA_KEYWORDS:
                changed |= fix_schema(v)
    elif isinstance(node, list):
        for v in node:
            changed |= fix_schema(v)
    return changed


def strip_ttl(blocks):
    """Meta rejects cache_control.ttl; drop it so the marker uses Meta's default TTL."""
    changed = False
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict):
            continue
        cc = b.get("cache_control")
        if isinstance(cc, dict) and "ttl" in cc:
            del cc["ttl"]
            changed = True
        if b.get("type") == "tool_result":
            changed |= strip_ttl(b.get("content"))
    return changed


def adapt_for_meta(data):
    """Make an Anthropic request body acceptable to Meta. Returns True if changed."""
    # Claude Code extension blocks (tool_addition, ...) and Anthropic-only fields.
    changed = muse_responses.normalize_for_messages(data)
    changed |= strip_ttl(data.get("system")) | strip_ttl(data.get("tools"))
    for msg in data.get("messages") or []:
        if isinstance(msg, dict):
            changed |= strip_ttl(msg.get("content"))
    for tool in data.get("tools") or []:
        if isinstance(tool, dict):
            changed |= fix_schema(tool.get("input_schema"))
    return changed


def meta_key():
    global _meta_key
    if _meta_key is None:
        # Env first so Windows (User env var) and containers work with no keychain.
        # On macOS the installer still prefers Keychain service "meta-model-api".
        env_key = os.environ.get("META_API_KEY", "").strip()
        if env_key:
            _meta_key = env_key
        else:
            _meta_key = subprocess.run(
                ["security", "find-generic-password", "-a", getpass.getuser(),
                 "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, check=True).stdout.strip()
    return _meta_key


class Router(BaseHTTPRequestHandler):
    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        data, model = None, ""
        if body:
            try:
                data = json.loads(body)
                model = data.get("model", "") or ""
            except (ValueError, AttributeError):
                data = None

        headers = {k.lower(): v for k, v in self.headers.items() if k.lower() not in SKIP}
        host = ANTHROPIC_HOST
        if model.startswith(MUSE_PREFIX):
            host = META_HOST
            # Deterministic re-serialization, so the cached prompt prefix stays stable.
            if isinstance(data, dict) and adapt_for_meta(data):
                body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            headers.pop("x-api-key", None)
            try:
                headers["authorization"] = "Bearer " + meta_key()
            except subprocess.CalledProcessError:
                self.send_error(502, "Meta API key not found in Keychain (service %s)" % KEYCHAIN_SERVICE)
                return
            # Prefer the Responses API, which supports prompt_cache_key; fall back to
            # Meta's /v1/messages for anything the translator can't map exactly.
            if (MUSE_API == "responses" and self.command == "POST" and isinstance(data, dict)
                    and self.path.split("?")[0] == "/v1/messages"):
                try:
                    muse_responses.serve(self, data, headers["authorization"])
                    return
                except muse_responses.Unsupported as e:
                    sys.stderr.write("  muse via messages (fallback: %s)\n" % e)
            # The claude.ai OAuth beta flag only means something to Anthropic.
            if "anthropic-beta" in headers:
                betas = [b for b in headers["anthropic-beta"].split(",")
                         if b.strip() and not b.strip().startswith("oauth-")]
                if betas:
                    headers["anthropic-beta"] = ",".join(betas)
                else:
                    del headers["anthropic-beta"]

        conn = http.client.HTTPSConnection(host, timeout=600)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            self.send_error(502, "upstream %s unreachable: %s" % (host, e))
            return

        try:
            err_body = None
            if resp.status >= 400:
                err_body = resp.read()
                sys.stderr.write("  upstream error: host=%s model=%s status=%s body=%s\n"
                                 % (host, model or "-", resp.status, err_body[:400].decode(errors="replace")))
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in SKIP:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                if err_body is not None:
                    self.wfile.write(err_body)
                while chunk := resp.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Claude Code cancelled the request.
        finally:
            conn.close()

    do_GET = do_POST = do_HEAD = _proxy

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    print("claude router on 127.0.0.1:%d" % PORT, file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Router).serve_forever()
