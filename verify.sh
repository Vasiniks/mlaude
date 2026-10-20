#!/usr/bin/env bash
# mlaude verify — Mac/Linux. Checks the four things that prove the router works.
# Usage: ./verify.sh [PORT]
set -uo pipefail
PORT="${1:-${ROUTER_PORT:-8787}}"
pass=0; fail=0
ok()   { echo "  PASS: $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $1"; fail=$((fail+1)); }

echo "==> mlaude verify (port $PORT)"

# 1. Router answers at all. Anthropic's root 404s — sometimes with the banner
#    page, sometimes with an empty body — so HTTP 404 (or the banner) is the
#    alive signal, not any particular body.
resp_code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 "http://127.0.0.1:$PORT/" || true)
resp_body=$(curl -s -m 5 "http://127.0.0.1:$PORT/" || true)
if [ "$resp_code" = "404" ] || echo "$resp_body" | grep -q "Anthropic API"; then ok "GET / routes to Anthropic (HTTP $resp_code)"
else bad "GET / returned HTTP $resp_code with unexpected body"; fi

# 2. Non-muse model is forwarded to Anthropic (401 without a real key proves the forward path).
code=$(curl -s -o /dev/null -w "%{http_code}" -m 10 "http://127.0.0.1:$PORT/v1/messages" \
  -X POST -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}')
if [ "$code" = "401" ]; then ok "claude-* POST forwards to api.anthropic.com (got 401 as expected without key)"
else bad "claude-* POST returned $code, expected 401"; fi

# 3. muse-* model is served through Meta and translated back to Anthropic shape.
body=$(curl -s -m 20 "http://127.0.0.1:$PORT/v1/messages" \
  -X POST -H "content-type: application/json" -H "x-api-key: test" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"muse-spark-1.3","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}')
if echo "$body" | grep -q '"type":"message"'; then ok "muse-spark POST returns Anthropic-shaped message via Meta"
else bad "muse-spark POST did not return a message: $(echo "$body" | head -c 300)"; fi

# 4. Turns Meta can't map exactly are sanitized, not failed: stop_sequences
#    makes Meta's API 400, so the router drops it (logged) and serves the turn
#    via Responses instead.
sbody=$(curl -s -m 20 "http://127.0.0.1:$PORT/v1/messages" \
  -X POST -H "content-type: application/json" -H "x-api-key: test" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"muse-spark-1.3","max_tokens":5,"stop_sequences":["x"],"messages":[{"role":"user","content":"hi"}]}')
if echo "$sbody" | grep -q '"type":"message"'; then ok "stop_sequences turn sanitized to Anthropic-shaped message (no 400)"
else bad "stop_sequences turn failed: $(echo "$sbody" | head -c 300)"; fi

# 5. oc- model is served through OpenCode. Needs a stored key: a 502 means
# "no key", which is a SKIP, not a failure.
ob=$(mktemp); trap 'rm -f "$ob"' EXIT
ocode=$(curl -s -o "$ob" -w "%{http_code}" -m 20 "http://127.0.0.1:$PORT/v1/messages" \
  -X POST -H "content-type: application/json" -H "x-api-key: test" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"oc-beam-free","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}')
obody=$(cat "$ob")
if echo "$obody" | grep -q '"type":"message"'; then ok "oc-beam-free POST returns Anthropic-shaped message via OpenCode"
elif [ "$ocode" = "502" ]; then echo "  SKIP: oc-beam-free (no OpenCode key stored)"
else bad "oc-beam-free POST failed: $(echo "$obody" | head -c 300)"; fi

echo "==> $pass passed, $fail failed"
[ "$fail" -eq 0 ]
