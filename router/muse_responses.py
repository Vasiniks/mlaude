"""Anthropic Messages <-> Meta Responses translation for Muse models.

Meta's Anthropic-compatible /v1/messages endpoint has no way to pin requests to
a prompt cache, so most turns bill as uncached input. Its Responses API accepts
prompt_cache_key, and keeps reasoning across turns through encrypted replay.
This module serves a Claude Code /v1/messages request through /v1/responses and
converts the reply (streamed or not) back into Anthropic format.

to_responses() raises Unsupported for anything it can't map exactly; the router
then forwards that request to /v1/messages unchanged.
"""
import hashlib
import http.client
import json
import queue
import sys
import threading
import time

META_HOST = "api.meta.ai"
CACHE_KEY = "claude-code"
PING_SECONDS = 10
# Meta occasionally accepts a request and then goes silent. The pings below keep
# Claude Code waiting forever, so give up after this long with no upstream bytes
# and send a retryable error instead.
STALL_SECONDS = 180
# Hard cap on one streamed reply (catches runaway generations that never go silent).
MAX_STREAM_SECONDS = 900
EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
               529: "overloaded_error"}
PASS_HEADERS = ("retry-after", "x-should-retry", "x-request-id")


class Unsupported(Exception):
    pass


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


# User/system block types the Anthropic API defines. Anything else is a Claude
# Code extension (e.g. tool_addition in subagent prompts) and gets mapped to text.
STANDARD_USER_BLOCKS = {"text", "image", "document", "tool_result", "search_result", "container_upload"}
# Anthropic request fields that have no Meta equivalent; dropped, not fatal.
DROP_FIELDS = ("safeguards",)
_seen_unknown = set()


def _note_unknown(b):
    t = b.get("type")
    if t not in _seen_unknown:
        _seen_unknown.add(t)
        sys.stderr.write("  muse: mapped user block %r to text: %s\n" % (t, _dumps(b)[:600]))


def _embedded_tools(node, out):
    """Collect Anthropic tool definitions ({name, input_schema}) nested anywhere in node."""
    if isinstance(node, dict):
        if isinstance(node.get("name"), str) and isinstance(node.get("input_schema"), dict):
            out.append(node)
            return
        for v in node.values():
            _embedded_tools(v, out)
    elif isinstance(node, list):
        for v in node:
            _embedded_tools(v, out)


def block_text(b):
    """Readable text for a block type Meta has no equivalent for."""
    if isinstance(b.get("text"), str):
        return b["text"]
    if isinstance(b.get("content"), str):
        return b["content"]
    tools = []
    _embedded_tools(b, tools)
    if tools:
        return "[%s] Tools now available: %s" % (b.get("type"), ", ".join(t["name"] for t in tools))
    rest = {k: v for k, v in b.items() if k not in ("type", "cache_control")}
    return "[%s] %s" % (b.get("type"), _dumps(rest))


def extension_blocks(data):
    """Yield every non-standard user/system block in the request."""
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "system"):
            continue
        for b in _blocks(m.get("content")):
            if isinstance(b, dict) and b.get("type") not in STANDARD_USER_BLOCKS:
                yield b


def merge_embedded_tools(data):
    """Append tool definitions carried inside extension blocks to data["tools"]. Returns True if changed."""
    found = []
    for b in extension_blocks(data):
        _embedded_tools(b, found)
    if not found:
        return False
    tools = data.setdefault("tools", [])
    have = {t.get("name") for t in tools if isinstance(t, dict)}
    changed = False
    for t in found:
        if t["name"] not in have:
            tools.append({k: v for k, v in t.items() if k != "defer_loading"})
            have.add(t["name"])
            changed = True
    return changed


def normalize_for_messages(data):
    """Make a request safe for Meta's /v1/messages fallback. Returns True if changed."""
    changed = False
    for f in DROP_FIELDS:
        if f in data:
            del data[f]
            changed = True
    changed |= merge_embedded_tools(data)
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "system"):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for i, b in enumerate(content):
            if isinstance(b, dict) and b.get("type") not in STANDARD_USER_BLOCKS:
                _note_unknown(b)
                content[i] = {"type": "text", "text": block_text(b)}
                changed = True
    return changed


# ---------------------------------------------------------------- request

def _media_url(block):
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return "data:%s;base64,%s" % (src.get("media_type"), src.get("data"))
    if src.get("type") == "url":
        return src.get("url")
    raise Unsupported("media source %r" % src.get("type"))


def _input_part(b):
    t = b.get("type")
    if t == "text":
        return {"type": "input_text", "text": b.get("text", "")}
    if t == "image":
        return {"type": "input_image", "image_url": _media_url(b)}
    if t == "document":
        src = b.get("source") or {}
        if src.get("type") == "text":
            return {"type": "input_text", "text": src.get("data", "")}
        if src.get("type") == "base64":
            return {"type": "input_file", "filename": b.get("title") or "document.pdf",
                    "file_data": _media_url(b)}
        raise Unsupported("document source %r" % src.get("type"))
    if t not in STANDARD_USER_BLOCKS or t == "search_result":
        _note_unknown(b)
        return {"type": "input_text", "text": block_text(b)}
    raise Unsupported("user block %r" % t)


def _user_turn(blocks, role, items):
    parts, tool_images = [], []
    for b in blocks:
        if b.get("type") == "tool_result":
            texts = []
            for p in _blocks(b.get("content")):
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "image":
                    tool_images.append(_input_part(p))
                elif p.get("type") == "tool_reference":
                    texts.append("Tool loaded: %s" % p.get("tool_name"))
                else:
                    raise Unsupported("tool_result block %r" % p.get("type"))
            out = "\n".join(texts)
            if b.get("is_error"):
                out = "[tool error] " + out
            items.append({"type": "function_call_output", "call_id": b.get("tool_use_id"),
                          "output": out or "(no output)"})
        else:
            parts.append(_input_part(b))
    if tool_images:
        parts = [{"type": "input_text", "text": "Images returned by the tool calls above:"}] + tool_images + parts
    if parts:
        items.append({"type": "message", "role": role, "content": parts})


def _assistant_turn(blocks, items):
    last_tool = max((i for i, b in enumerate(blocks) if b.get("type") == "tool_use"), default=-1)
    dangling_reasoning = False
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t in ("thinking", "redacted_thinking"):
            enc = b.get("data") if t == "redacted_thinking" else b.get("signature")
            if enc:
                items.append({"type": "reasoning", "summary": [], "encrypted_content": enc})
                dangling_reasoning = True
        elif t == "text":
            if not b.get("text"):
                continue
            msg = {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": b["text"]}]}
            if i < last_tool:
                msg["phase"] = "commentary"  # Meta requires this for text before a function_call
            items.append(msg)
            dangling_reasoning = False
        elif t == "tool_use":
            items.append({"type": "function_call", "call_id": b.get("id"), "name": b.get("name"),
                          "arguments": _dumps(b.get("input") or {})})
            dangling_reasoning = False
        else:
            raise Unsupported("assistant block %r" % t)
    if dangling_reasoning:
        # A reasoning item must be followed by an assistant message or function_call.
        items.append({"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": ""}]})


def to_responses(data):
    """Return (responses_body, summarize_thinking) or raise Unsupported."""
    for field in ("stop_sequences", "top_k", "container", "mcp_servers"):
        if data.get(field):
            raise Unsupported(field)
    data = dict(data)
    for field in DROP_FIELDS:
        data.pop(field, None)
    data["tools"] = list(data.get("tools") or [])
    merge_embedded_tools(data)
    tool_choice = data.get("tool_choice") or {}
    if tool_choice.get("type") not in (None, "auto"):
        raise Unsupported("tool_choice %r" % tool_choice.get("type"))

    # Deferred tools stay invisible to the model until tool search loads them,
    # which Claude Code records as tool_reference blocks in tool results.
    loaded = {p.get("tool_name") for m in data.get("messages") or [] for b in _blocks(m.get("content"))
              if isinstance(b, dict) and b.get("type") == "tool_result"
              for p in _blocks(b.get("content")) if isinstance(p, dict) and p.get("type") == "tool_reference"}
    tools = []
    for t in data.get("tools") or []:
        if t.get("type") not in (None, "custom"):
            raise Unsupported("tool type %r" % t.get("type"))
        if t.get("defer_loading") and t.get("name") not in loaded:
            continue
        if str(t.get("name", "")).count(".") > 1:
            raise Unsupported("tool name %r" % t.get("name"))
        tools.append({"type": "function", "name": t["name"], "description": t.get("description", ""),
                      "parameters": t.get("input_schema") or {"type": "object"}})

    items = []
    for m in data.get("messages") or []:
        role = m.get("role")
        blocks = _blocks(m.get("content"))
        if role == "assistant":
            _assistant_turn(blocks, items)
        elif role == "user":
            _user_turn(blocks, "user", items)
        elif role == "system":
            _user_turn(blocks, "developer", items)
        else:
            raise Unsupported("role %r" % role)

    body = {"model": data["model"], "input": items, "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": CACHE_KEY, "prompt_cache_retention": "24h",
            "max_output_tokens": max(16, int(data.get("max_tokens") or 4096)),
            "stream": bool(data.get("stream"))}
    system = [b.get("text", "") for b in _blocks(data.get("system")) if b.get("type") == "text"]
    if system:
        body["instructions"] = "\n\n".join(system)
    if tools:
        body["tools"] = tools
        if tool_choice.get("disable_parallel_tool_use"):
            body["parallel_tool_calls"] = False
    for k in ("temperature", "top_p"):
        if data.get(k) is not None:
            body[k] = data[k]

    reasoning = {}
    output_config = data.get("output_config") or {}
    if output_config.get("effort") in EFFORTS:
        reasoning["effort"] = EFFORTS[output_config["effort"]]
    thinking = data.get("thinking") or {}
    summarize = thinking.get("type") in ("enabled", "adaptive") and thinking.get("display") != "omitted"
    if summarize:
        reasoning["summary"] = "auto"
    if reasoning:
        body["reasoning"] = reasoning
    fmt = output_config.get("format")
    if fmt:
        if fmt.get("type") != "json_schema":
            raise Unsupported("output format %r" % fmt.get("type"))
        body["text"] = {"format": {"type": "json_schema", "name": "output", "schema": fmt.get("schema")}}
    user_id = (data.get("metadata") or {}).get("user_id")
    if user_id:
        body["safety_identifier"] = hashlib.sha256(str(user_id).encode()).hexdigest()
    return body, summarize


# ---------------------------------------------------------------- response

def _usage(u):
    u = u or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    return {"input_tokens": max(0, (u.get("input_tokens") or 0) - cached),
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached,
            "output_tokens": u.get("output_tokens") or 0}


def _stop_reason(resp, saw_tool):
    if resp.get("status") == "incomplete":
        return "max_tokens"
    return "tool_use" if saw_tool else "end_turn"


def _args(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"raw_arguments": raw}


def from_response(resp, model, summarize):
    content, saw_tool = [], False
    for it in resp.get("output") or []:
        t = it.get("type")
        if t == "reasoning" and it.get("encrypted_content"):
            if summarize:
                summary = "".join(s.get("text", "") for s in it.get("summary") or [])
                content.append({"type": "thinking", "thinking": summary, "signature": it["encrypted_content"]})
            else:
                content.append({"type": "redacted_thinking", "data": it["encrypted_content"]})
        elif t == "message":
            text = "".join(p.get("text", "") for p in it.get("content") or [] if p.get("type") == "output_text")
            if text:
                content.append({"type": "text", "text": text})
        elif t == "function_call":
            content.append({"type": "tool_use", "id": it.get("call_id"), "name": it.get("name"),
                            "input": _args(it.get("arguments"))})
            saw_tool = True
    return {"id": "msg_" + str(resp.get("id", "")), "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": _stop_reason(resp, saw_tool), "stop_sequence": None,
            "usage": _usage(resp.get("usage"))}


def _sse(event, payload):
    return ("event: %s\ndata: %s\n\n" % (event, _dumps(payload))).encode()


class StreamTranslator:
    """Turns Responses stream events into Anthropic stream events."""

    def __init__(self, model, summarize, est_input=0):
        self.model, self.summarize = model, summarize
        # Meta reports usage only at the end; message_start carries this estimate so
        # Claude Code's live token counters move. message_delta replaces it with the real numbers.
        self.est_input = est_input
        self.open = {}        # output_index -> (anthropic block index, kind)
        self.item_index = {}  # item_id -> output_index
        self.next_block = 0
        self.args_seen = set()
        self.saw_tool = False
        self.started = False
        self.finished = False
        self.usage = None

    def _start(self, rid):
        self.started = True
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": "msg_" + str(rid), "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.est_input, "output_tokens": 1, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}})]

    def _block_start(self, oi, kind, block):
        idx = self.next_block
        self.next_block += 1
        self.open[oi] = (idx, kind)
        return _sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": block})

    def _delta(self, oi, delta):
        if oi not in self.open:
            return []
        return [_sse("content_block_delta", {"type": "content_block_delta", "index": self.open[oi][0], "delta": delta})]

    def _block_stop(self, oi):
        idx, _ = self.open.pop(oi)
        return _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    def _oi(self, e):
        if "output_index" in e:
            return e["output_index"]
        return self.item_index.get(e.get("item_id"))

    def handle(self, e):
        t = e.get("type", "")
        out = []
        if not self.started and t.startswith("response."):
            out += self._start((e.get("response") or {}).get("id", "stream"))
        if t == "response.output_item.added":
            item, oi = e.get("item") or {}, e.get("output_index")
            if item.get("id"):
                self.item_index[item["id"]] = oi
            kind = item.get("type")
            if kind == "message":
                out.append(self._block_start(oi, "text", {"type": "text", "text": ""}))
            elif kind == "function_call":
                self.saw_tool = True
                out.append(self._block_start(oi, "tool", {"type": "tool_use", "id": item.get("call_id"),
                                                          "name": item.get("name"), "input": {}}))
            elif kind == "reasoning" and self.summarize:
                out.append(self._block_start(oi, "thinking", {"type": "thinking", "thinking": "", "signature": ""}))
        elif t == "response.output_text.delta":
            out += self._delta(self._oi(e), {"type": "text_delta", "text": e.get("delta", "")})
        elif t == "response.function_call_arguments.delta":
            oi = self._oi(e)
            self.args_seen.add(oi)
            out += self._delta(oi, {"type": "input_json_delta", "partial_json": e.get("delta", "")})
        elif t == "response.reasoning_summary_text.delta":
            out += self._delta(self._oi(e), {"type": "thinking_delta", "thinking": e.get("delta", "")})
        elif t == "response.output_item.done":
            item, oi = e.get("item") or {}, e.get("output_index")
            kind = item.get("type")
            if kind == "function_call" and oi not in self.args_seen and item.get("arguments"):
                out += self._delta(oi, {"type": "input_json_delta", "partial_json": item["arguments"]})
            if kind == "reasoning":
                enc = item.get("encrypted_content")
                if self.summarize and oi in self.open:
                    out += self._delta(oi, {"type": "signature_delta", "signature": enc or ""})
                elif enc:
                    out.append(self._block_start(oi, "redacted", {"type": "redacted_thinking", "data": enc}))
            if oi in self.open:
                out.append(self._block_stop(oi))
        elif t in ("response.completed", "response.incomplete"):
            resp = e.get("response") or {}
            for oi in list(self.open):
                out.append(self._block_stop(oi))
            self.usage = resp.get("usage")
            out.append(_sse("message_delta", {"type": "message_delta",
                                              "delta": {"stop_reason": _stop_reason(resp, self.saw_tool),
                                                        "stop_sequence": None},
                                              "usage": _usage(self.usage)}))
            out.append(_sse("message_stop", {"type": "message_stop"}))
            self.finished = True
        elif t in ("response.failed", "error"):
            err = (e.get("response") or {}).get("error") or e.get("error") or e
            out.append(_sse("error", {"type": "error", "error": {
                "type": "api_error", "message": (err or {}).get("message") or "upstream stream failed"}}))
            self.finished = True
        return out


# ---------------------------------------------------------------- transport

def _anthropic_error(status, raw):
    try:
        err = json.loads(raw).get("error") or {}
        msg = err.get("message") or raw.decode(errors="replace")
        param = err.get("param") or ""
    except (ValueError, AttributeError):
        msg, param = raw.decode(errors="replace"), ""
    low = msg.lower()
    if status == 400 and ("context" in low and ("window" in low or "length" in low) or "too long" in low):
        msg = "prompt is too long: " + msg  # lets Claude Code compact and retry
    elif status == 400 and ("encrypted" in low or "reasoning" in str(param).lower()):
        msg += " (Invalid `signature` in `thinking` block)"  # lets Claude Code drop old thinking
    etype = ERROR_TYPES.get(status, "api_error")
    return _dumps({"type": "error", "error": {"type": etype, "message": msg}}).encode()


def _log(body, usage):
    u = usage or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    sys.stderr.write("  muse via responses: input=%s cached=%s output=%s\n"
                     % (u.get("input_tokens"), cached, u.get("output_tokens")))


def serve(handler, data, auth):
    """Serve one Claude Code request through Meta's Responses API. Raises Unsupported."""
    body, summarize = to_responses(data)
    model = data["model"]
    conn = http.client.HTTPSConnection(META_HOST, timeout=STALL_SECONDS)
    try:
        conn.request("POST", "/v1/responses", body=_dumps(body).encode(),
                     headers={"authorization": auth, "content-type": "application/json",
                              "accept": "text/event-stream" if body["stream"] else "application/json"})
        resp = conn.getresponse()
        conn.sock.settimeout(600)  # the stream loop below enforces STALL_SECONDS itself
    except OSError as e:
        handler.send_error(502, "upstream %s unreachable: %s" % (META_HOST, e))
        return

    try:
        if resp.status != 200:
            raw = resp.read()
            sys.stderr.write("  upstream error: host=%s/v1/responses model=%s status=%s body=%s\n"
                             % (META_HOST, model, resp.status, raw[:400].decode(errors="replace")))
            payload = _anthropic_error(resp.status, raw)
            handler.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in PASS_HEADERS:
                    handler.send_header(k, v)
            handler.send_header("content-type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            return

        if not body["stream"]:
            result = json.loads(resp.read())
            _log(body, result.get("usage"))
            payload = _dumps(from_response(result, model, summarize)).encode()
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            return

        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        events = queue.Queue()

        def reader():
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == b"[DONE]":
                        break
                    try:
                        events.put(json.loads(chunk))
                    except ValueError:
                        pass
            except (OSError, AttributeError, ValueError):
                pass  # connection closed under us (stream finished or client cancelled)
            events.put(None)

        threading.Thread(target=reader, daemon=True).start()
        tr = StreamTranslator(model, summarize, est_input=len(_dumps(body)) // 4)
        started = last_event = time.monotonic()

        def abort(why):
            sys.stderr.write("  muse: %s, aborting so Claude Code retries\n" % why)
            handler.wfile.write(_sse("error", {"type": "error", "error": {
                "type": "overloaded_error", "message": "Meta stream %s; retry" % why}}))
            handler.wfile.flush()

        while True:
            if time.monotonic() - started >= MAX_STREAM_SECONDS:
                abort("exceeded %ds" % MAX_STREAM_SECONDS)
                break
            try:
                e = events.get(timeout=PING_SECONDS)
            except queue.Empty:
                if time.monotonic() - last_event >= STALL_SECONDS:
                    abort("silent for %ds" % STALL_SECONDS)
                    break
                handler.wfile.write(_sse("ping", {"type": "ping"}))  # keeps Claude Code's idle watchdog fed
                handler.wfile.flush()
                continue
            last_event = time.monotonic()
            if e is None:
                break
            for chunk in tr.handle(e):
                handler.wfile.write(chunk)
            handler.wfile.flush()
            if tr.finished:
                break
        _log(body, tr.usage)
    except (BrokenPipeError, ConnectionResetError):
        pass  # Claude Code cancelled the request.
    finally:
        conn.close()
