"""Anthropic Messages <-> OpenAI chat/completions translation for oc- models.

Mirrors muse_responses.py: to_openai() builds the upstream body (raising
Unsupported for anything with no OpenAI equivalent), from_openai() converts
a full reply, and StreamTranslator turns OpenAI SSE deltas into Anthropic
stream events. serve() POSTs to the configured host/path and answers the
waiting Claude Code handler in Anthropic shape (streamed or not).
"""
import http.client
import json
import queue
import sys
import threading
import time

OC_PREFIX = "oc-"
PING_SECONDS = 10
# Same stall guards as the Meta path: OpenAI-compatible endpoints sometimes
# accept a request and go silent; fail retryably instead of hanging forever.
STALL_SECONDS = 180
MAX_STREAM_SECONDS = 900
ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error",
               403: "permission_error", 404: "not_found_error",
               413: "request_too_large", 429: "rate_limit_error",
               529: "overloaded_error"}


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


def _note(what, why):
    sys.stderr.write("  oc: %s (%s)\n" % (what, why))


def _text_of(blocks):
    return "\n".join(p.get("text", "") for p in _blocks(blocks)
                     if p.get("type") == "text")


def _media_url(block):
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return "data:%s;base64,%s" % (src.get("media_type"), src.get("data"))
    if src.get("type") == "url":
        return src.get("url")
    raise Unsupported("media source %r" % src.get("type"))


def _user_parts(blocks):
    """Split user content into OpenAI parts (text/image), skipping the rest."""
    parts = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            parts.append({"type": "image_url",
                          "image_url": {"url": _media_url(b)}})
        elif t == "document":
            src = b.get("source") or {}
            if src.get("type") == "text":
                parts.append({"type": "text", "text": src.get("data", "")})
            else:
                raise Unsupported("document source %r" % src.get("type"))
        else:
            raise Unsupported("user block %r" % t)
    return parts


def to_openai(data):
    """Return (openai_body, upstream_model) or raise Unsupported."""
    model = data.get("model", "") or ""
    if not model.startswith(OC_PREFIX):
        raise Unsupported("model %r is not oc-" % model)
    upstream = model[len(OC_PREFIX):]
    if not upstream:
        raise Unsupported("empty model after oc- prefix")

    messages = []
    system = [b.get("text", "") for b in _blocks(data.get("system"))
              if b.get("type") == "text"]
    if system:
        messages.append({"role": "system",
                         "content": "\n\n".join(system)})

    for m in data.get("messages") or []:
        role = m.get("role")
        blocks = _blocks(m.get("content"))
        if role == "user":
            texts, tools = [], []
            for b in blocks:
                if b.get("type") == "tool_result":
                    texts_out = _text_of(b.get("content"))
                    if b.get("is_error"):
                        texts_out = "[tool error] " + texts_out
                    tools.append({"role": "tool",
                                  "tool_call_id": b.get("tool_use_id"),
                                  "content": texts_out or "(no output)"})
                else:
                    texts.append(b)
            if texts:
                messages.append({"role": "user",
                                 "content": _user_parts(texts)})
            messages.extend(tools)
        elif role == "assistant":
            content, calls = [], []
            for b in blocks:
                t = b.get("type")
                if t == "text":
                    if b.get("text"):
                        content.append({"type": "text",
                                        "text": b["text"]})
                elif t == "tool_use":
                    calls.append({"id": b.get("id"), "type": "function",
                                  "function": {
                                      "name": b.get("name"),
                                      "arguments": _dumps(
                                          b.get("input") or {})}})
                elif t in ("thinking", "redacted_thinking"):
                    # OpenAI chat has no reasoning passthrough; the thought
                    # is already summarized in the following text, if any.
                    _note("dropped %s block" % t,
                          "OpenAI chat can't carry it")
                else:
                    raise Unsupported("assistant block %r" % t)
            msg = {"role": "assistant"}
            if content:
                msg["content"] = content
            if calls:
                msg["tool_calls"] = calls
            if content or calls:
                messages.append(msg)
        elif role == "system":
            messages.append({"role": "system",
                             "content": _text_of(blocks)})
        else:
            raise Unsupported("role %r" % role)

    body = {"model": upstream, "messages": messages,
            "stream": bool(data.get("stream"))}
    for k in ("temperature", "top_p"):
        if data.get(k) is not None:
            body[k] = data[k]
    if data.get("max_tokens") is not None:
        body["max_tokens"] = data["max_tokens"]
    if data.get("stop_sequences"):
        body["stop"] = list(data["stop_sequences"])

    tools = []
    for t in data.get("tools") or []:
        if t.get("type") not in (None, "custom"):
            raise Unsupported("tool type %r" % t.get("type"))
        tools.append({"type": "function",
                      "function": {"name": t.get("name"),
                                   "description": t.get("description", ""),
                                   "parameters": t.get("input_schema") or
                                   {"type": "object"}}})
    if tools:
        body["tools"] = tools
        choice = data.get("tool_choice") or {}
        ctype = choice.get("type")
        if ctype == "auto" or ctype is None:
            body["tool_choice"] = "auto"
        elif ctype == "any":
            body["tool_choice"] = "required"
        else:
            raise Unsupported("tool_choice %r" % ctype)
    return body, upstream


# ---------------------------------------------------------------- response

def _usage(u):
    u = u or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {"input_tokens": max(0, (u.get("prompt_tokens") or 0) - cached),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached,
            "output_tokens": u.get("completion_tokens") or 0}


def _stop_reason(finish):
    if finish == "tool_calls":
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    return "end_turn"


def _args(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"raw_arguments": raw}


def from_openai(resp, model):
    content, finish = [], "stop"
    choices = resp.get("choices") or []
    if choices:
        msg = (choices[0].get("message") or {})
        finish = choices[0].get("finish_reason") or "stop"
        text = msg.get("content") or ""
        if text:
            content.append({"type": "text", "text": text})
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            content.append({"type": "tool_use",
                            "id": call.get("id"),
                            "name": fn.get("name"),
                            "input": _args(fn.get("arguments"))})
    return {"id": "msg_" + str(resp.get("id", "")), "type": "message",
            "role": "assistant", "model": model, "content": content,
            "stop_reason": _stop_reason(finish), "stop_sequence": None,
            "usage": _usage(resp.get("usage"))}


def _sse(event, payload):
    return ("event: %s\ndata: %s\n\n" % (event, _dumps(payload))).encode()


class StreamTranslator:
    """Turns OpenAI chat SSE chunks into Anthropic stream events."""

    def __init__(self, model):
        self.model = model
        self.started = False
        self.finished = False
        self.text_open = False
        self.text_block = None
        self.tools = {}  # openai index -> {id, name, block, opened}
        self.next_block = 0
        self.saw_tool = False
        self.finish = "stop"

    def _start(self, rid):
        self.started = True
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": "msg_" + str(rid), "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 1,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}})]

    def _open_text(self):
        self.text_open = True
        self.text_block = self.next_block
        self.next_block += 1
        return _sse("content_block_start", {
            "type": "content_block_start", "index": self.text_block,
            "content_block": {"type": "text", "text": ""}})

    def handle(self, chunk):
        out = []
        if not self.started:
            out += self._start(chunk.get("id", "stream"))
        choices = chunk.get("choices") or []
        if not choices:
            return out
        first = choices[0]
        delta = first.get("delta") or {}
        if first.get("finish_reason"):
            self.finish = first["finish_reason"] or "stop"
            if self.text_open:
                out.append(_sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": self.text_block}))
                self.text_open = False
            for idx in sorted(self.tools):
                st = self.tools[idx]
                if st["opened"]:
                    out.append(_sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": st["block"]}))
                    st["opened"] = False
            out.append(_sse("message_delta", {"type": "message_delta",
                                              "delta": {
                                                  "stop_reason":
                                                  _stop_reason(self.finish),
                                                  "stop_sequence": None},
                                              "usage": _usage(
                                                  chunk.get("usage"))}))
            out.append(_sse("message_stop", {"type": "message_stop"}))
            self.finished = True
            return out
        if delta.get("content"):
            if not self.text_open:
                out.append(self._open_text())
            out.append(_sse("content_block_delta", {
                "type": "content_block_delta", "index": self.text_block,
                "delta": {"type": "text_delta",
                          "text": delta["content"]}}))
        for call in delta.get("tool_calls") or []:
            idx = call.get("index", 0)
            st = self.tools.setdefault(
                idx, {"id": "", "name": "", "block": None, "opened": False})
            if call.get("id"):
                st["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                st["name"] = fn["name"]
            if not st["opened"] and st["id"] and st["name"]:
                st["block"] = self.next_block
                self.next_block += 1
                st["opened"] = True
                self.saw_tool = True
                out.append(_sse("content_block_start", {
                    "type": "content_block_start", "index": st["block"],
                    "content_block": {"type": "tool_use", "id": st["id"],
                                      "name": st["name"], "input": {}}}))
            args = fn.get("arguments")
            if args and st["opened"]:
                out.append(_sse("content_block_delta", {
                    "type": "content_block_delta", "index": st["block"],
                    "delta": {"type": "input_json_delta",
                              "partial_json": args}}))
        return out


# ---------------------------------------------------------------- transport

def _openai_error(status, raw):
    try:
        err = json.loads(raw).get("error") or {}
        msg = err.get("message") or raw.decode(errors="replace")
    except (ValueError, AttributeError):
        msg = raw.decode(errors="replace")
    low = msg.lower()
    if status == 400 and ("context" in low and ("length" in low or "window" in low)
                          or "too long" in low):
        msg = "prompt is too long: " + msg  # lets Claude Code compact and retry
    etype = ERROR_TYPES.get(status, "api_error")
    return _dumps({"type": "error",
                   "error": {"type": etype, "message": msg}}).encode()


def serve(handler, data, auth, host, path):
    """Serve one Claude Code request through an OpenAI-compatible endpoint."""
    body, _ = to_openai(data)
    model = data["model"]
    conn = http.client.HTTPSConnection(host, timeout=STALL_SECONDS)
    try:
        conn.request("POST", path, body=_dumps(body).encode(),
                     headers={"authorization": auth,
                              "content-type": "application/json",
                              "accept": "text/event-stream"
                              if body["stream"] else "application/json"})
        resp = conn.getresponse()
        conn.sock.settimeout(600)
    except OSError as e:
        handler.send_error(502, "upstream %s unreachable: %s" % (host, e))
        return

    try:
        if resp.status != 200:
            raw = resp.read()
            sys.stderr.write("  upstream error: host=%s%s model=%s status=%s body=%s\n"
                             % (host, path, model, resp.status,
                                raw[:400].decode(errors="replace")))
            payload = _openai_error(resp.status, raw)
            handler.send_response(resp.status)
            handler.send_header("content-type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            return

        if not body["stream"]:
            result = json.loads(resp.read())
            payload = _dumps(from_openai(result, model)).encode()
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
                pass
            events.put(None)

        threading.Thread(target=reader, daemon=True).start()
        tr = StreamTranslator(model)
        started = last_event = time.monotonic()

        def abort(why):
            sys.stderr.write("  oc: %s, aborting so Claude Code retries\n" % why)
            handler.wfile.write(_sse("error", {"type": "error", "error": {
                "type": "overloaded_error",
                "message": "OpenCode stream %s; retry" % why}}))
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
                handler.wfile.write(_sse("ping", {"type": "ping"}))
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
    except (BrokenPipeError, ConnectionResetError):
        pass  # Claude Code cancelled the request.
    finally:
        conn.close()
