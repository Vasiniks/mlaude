#!/usr/bin/env bash
# mlaude verify — Mac/Linux. Checks the three things that prove the router works.
# Usage: ./verify.sh [PORT]
set -uo pipefail
PORT="${1:-${ROUTER_PORT:-8787}}"
pass=0; fail=0
ok()   { echo "  PASS: $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $1"; fail=$((fail+1)); }

echo "==> mlaude verify (port $PORT)"

# 1. Router answers like the Anthropic API root (upstream returns 404 + banner, so check body, not status).
if curl -s -m 5 "http://127.0.0.1:$PORT/" | grep -q "Anthropic API"; then ok "GET / returns Anthropic banner (pass-through alive)"
else bad "GET / did not return Anthropic banner"; fi

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

echo "==> $pass passed, $fail failed"
[ "$fail" -eq 0 ]
