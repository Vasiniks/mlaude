"""Free-tier transport through the local OpenCode background service.

Direct HTTPS calls to opencode.ai/zen from a third-party client are rejected
with FreeTierError (the gateway only serves free models to genuine OpenCode
clients). But the OpenCode CLI on this machine *is* a genuine client: its
background service (`opencode serve --service`) reaches the same free models
fine. So for oc- requests we drive that service via `opencode api`
subprocesses (which handle auth + port discovery themselves):

    session.create -> session.switchModel -> session.prompt
    -> poll session.message.list until the idle outcome
    -> session.remove (cleanup)

No API key or billing needed; stdlib only. Raises LocalUpstreamError when
the local service is unavailable so the caller can fall back to direct HTTPS
(which still serves billed keys).
"""
import json
import shutil
import subprocess
import time

POLL_SECONDS = 3
DEADLINE_SECONDS = 540


class LocalUpstreamError(Exception):
    pass


def _run(*args):
    """Run `opencode api ...`, return parsed {"data": ...}. Raises LocalUpstreamError."""
    if shutil.which("opencode") is None:
        raise LocalUpstreamError("opencode binary not found")
    try:
        out = subprocess.run(["opencode", "api"] + list(args),
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise LocalUpstreamError("opencode api failed: %s" % e)
    if out.returncode != 0:
        err = (out.stderr or out.stdout or "").strip().splitlines()
        raise LocalUpstreamError("opencode api: %s" % (err[-1] if err else
                                                       "exit %d" % out.returncode))
    try:
        return json.loads(out.stdout or "{}")
    except ValueError as e:
        raise LocalUpstreamError("opencode api bad JSON: %s" % e)


def _text_of_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_result":
            inner = _text_of_content(b.get("content"))
            parts.append("[tool %s result%s]: %s" % (
                b.get("tool_use_id", "?"),
                " (error)" if b.get("is_error") else "", inner or "(no output)"))
        elif t == "tool_use":
            parts.append("[tool call %s %s]" % (b.get("name", "?"),
                                                json.dumps(b.get("input") or {})))
        elif t in ("thinking", "redacted_thinking"):
            pass
        elif t == "image":
            parts.append("[image omitted]")
    return "\n".join(p for p in parts if p)


def flatten_to_text(data):
    """Render an Anthropic Messages request as one plain-text prompt."""
    chunks = []
    system = _text_of_content(data.get("system"))
    if system.strip():
        chunks.append(system.strip())
    for m in data.get("messages") or []:
        role = m.get("role", "?")
        text = _text_of_content(m.get("content")).strip()
        if text:
            chunks.append("%s: %s" % (role, text))
    return "\n\n".join(chunks).strip()


def _assistant_text(messages, since):
    """Concatenate text parts of assistant messages created at/after `since`."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("type") != "assistant":
            continue
        if (m.get("time") or {}).get("created", 0) < since:
            continue
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                out.append(c["text"])
    return "\n".join(out)


def complete(upstream_model, prompt_text):
    """One full turn through the local OpenCode service. Returns reply text.

    Raises LocalUpstreamError (service down) or returns ("", error_message)
    text when the model itself errored (rate limit, etc).
    """
    if not prompt_text:
        raise LocalUpstreamError("empty prompt")
    sid = _run("session.create",
               "--data", json.dumps({"title": "mlaude"}))["data"]["id"]
    try:
        _run("session.switchModel", "--param", "sessionID=" + sid,
             "--data", json.dumps({"model": {"providerID": "opencode",
                                             "id": upstream_model}}))
        before = int(time.time() * 1000)
        _run("session.prompt", "--param", "sessionID=" + sid,
             "--data", json.dumps({"text": prompt_text}))
        deadline = time.monotonic() + DEADLINE_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise LocalUpstreamError("local opencode turn timed out")
            time.sleep(POLL_SECONDS)
            msgs = _run("session.message.list",
                        "--param", "sessionID=" + sid)["data"]
            idle = [m for m in msgs
                    if isinstance(m, dict) and m.get("type") == "idle"
                    and (m.get("time") or {}).get("created", 0) >= before]
            if not idle:
                continue
            outcome = idle[-1].get("outcome")
            text = _assistant_text(msgs, before).strip()
            if outcome == "succeeded":
                return text or "(empty reply)"
            err = idle[-1].get("error") or {}
            detail = err.get("message") or outcome or "unknown error"
            raise LocalUpstreamError("opencode %s: %s" % (upstream_model, detail))
    finally:
        try:
            _run("session.remove", "--param", "sessionID=" + sid)
        except LocalUpstreamError:
            pass
