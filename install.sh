#!/usr/bin/env bash
# mlaude installer — macOS (also works on Linux).
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/Vasiniks/mlaude/main/install.sh | bash
# Local:
#   ./install.sh
set -euo pipefail

GH_REPO="${GH_REPO:-Vasiniks/mlaude}"
BRANCH="${BRANCH:-main}"
PORT="${ROUTER_PORT:-8787}"
ROUTER_DIR="${ROUTER_DIR:-$HOME/.local/share/claude-router}"
PLIST="$HOME/Library/LaunchAgents/com.user.claude-router.plist"
KEYCHAIN_SERVICE="meta-model-api"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need python3
need curl

echo "==> mlaude: installing router to $ROUTER_DIR (port $PORT)"
mkdir -p "$ROUTER_DIR/router" "$ROUTER_DIR/app"

# 1. App files: use local copies when run from a clone, else download.
#    Installed layout mirrors the repo: router/ + app/ subdirectories.
ROUTER_FILES="router.py muse_responses.py oc_chat.py oc_local.py keys.py sync.py api.py connections.py ui.html test_router.py test_keys.py test_sync.py test_api.py test_oc_chat.py test_oc_local.py test_translator.py test_connections.py"
APP_FILES="main.py tray.py test_app.py"
if [ -f "$SCRIPT_DIR/router/router.py" ]; then
  for f in $ROUTER_FILES; do cp "$SCRIPT_DIR/router/$f" "$ROUTER_DIR/router/"; done
  for f in $APP_FILES; do cp "$SCRIPT_DIR/app/$f" "$ROUTER_DIR/app/"; done
  echo "    copied router + app files from local checkout"
else
  echo "    downloading files from github ($GH_REPO@$BRANCH)..."
  for f in $ROUTER_FILES; do
    curl -fsSL "https://raw.githubusercontent.com/$GH_REPO/$BRANCH/router/$f" -o "$ROUTER_DIR/router/$f"
  done
  for f in $APP_FILES; do
    curl -fsSL "https://raw.githubusercontent.com/$GH_REPO/$BRANCH/app/$f" -o "$ROUTER_DIR/app/$f"
  done
fi
python3 -m py_compile "$ROUTER_DIR/router/"*.py "$ROUTER_DIR/app/"*.py
echo "    compile OK"

# 1b. Tray deps (best-effort): without them the app runs headless and the
# dashboard stays the control surface.
if python3 -m pip install --user pystray pillow >/dev/null 2>&1; then
  echo "    tray deps installed"
else
  echo "    tray deps skipped (headless mode; dashboard still works)"
fi

# 2. Meta API key -> macOS Keychain (service: meta-model-api). Env fallback supported.
# API tokens never contain whitespace, so strip it all: heals broken pastes
# (stray newline/space) instead of storing a key Meta will reject.
PY_BIN="$(command -v python3)"
if [ -n "${META_API_KEY:-}" ]; then
  echo "    META_API_KEY env var is set; storing it in Keychain too..."
  META_KEY="$META_API_KEY"
elif security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w >/dev/null 2>&1; then
  META_KEY="$(security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w)"
else
  printf "    Paste your Meta API key (https://developers.meta.ai): "
  read -rs META_KEY
  echo ""
fi
META_KEY="$(printf '%s' "$META_KEY" | tr -d '[:space:]')"
[ -n "$META_KEY" ] || { echo "    empty key; aborting" >&2; exit 1; }
if security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  if [ "$(security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w)" != "$META_KEY" ]; then
    security add-generic-password -U -a "$USER" -s "$KEYCHAIN_SERVICE" -w "$META_KEY"
    echo "    updated Keychain ($KEYCHAIN_SERVICE)"
  else
    echo "    Meta key already in Keychain; leaving it alone"
  fi
else
  security add-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w "$META_KEY"
  echo "    stored in Keychain ($KEYCHAIN_SERVICE)"
fi

# 2b. OpenCode key (skippable — set later in the dashboard or via
# `mlaude keys set opencode`).
if [ -n "${OPENCODE_API_KEY:-}" ]; then
  echo "    OPENCODE_API_KEY env var is set; storing it in Keychain too..."
  OC_KEY="$OPENCODE_API_KEY"
elif security find-generic-password -a "$USER" -s "opencode-api" -w >/dev/null 2>&1; then
  OC_KEY="$(security find-generic-password -a "$USER" -s "opencode-api" -w)"
else
  printf "    Paste your OpenCode API key (Enter to skip, dashboard later): "
  read -rs OC_KEY || true
  echo ""
fi
OC_KEY="$(printf '%s' "$OC_KEY" | tr -d '[:space:]')"
if [ -z "$OC_KEY" ]; then
  echo "    skipped (dashboard or \`mlaude keys set opencode\` later)"
elif security find-generic-password -a "$USER" -s "opencode-api" >/dev/null 2>&1; then
  if [ "$(security find-generic-password -a "$USER" -s "opencode-api" -w)" != "$OC_KEY" ]; then
    security add-generic-password -U -a "$USER" -s "opencode-api" -w "$OC_KEY"
    echo "    updated Keychain (opencode-api)"
  else
    echo "    OpenCode key already in Keychain; leaving it alone"
  fi
else
  security add-generic-password -a "$USER" -s "opencode-api" -w "$OC_KEY"
  echo "    stored in Keychain (opencode-api)"
fi

# 3. LaunchAgent for the unified app (proxy child + tray), RunAtLoad + KeepAlive.
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.claude-router</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY_BIN</string>
    <string>$ROUTER_DIR/app/main.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ROUTER_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/claude-router.log</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/claude-router.log</string>
</dict>
</plist>
EOF
# (Re)load: new launchctl first, fall back to legacy.
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
echo "    LaunchAgent loaded"

# 4. Claude settings: merge snippet (env + model + picker + skillOverrides), preserving the rest.
SNIPPET=""
SNIPPET_TMP=""
trap '[ -n "$SNIPPET_TMP" ] && rm -f "$SNIPPET_TMP"' EXIT
if [ -f "$SCRIPT_DIR/config/claude-settings.snippet.json" ]; then
  SNIPPET="$SCRIPT_DIR/config/claude-settings.snippet.json"
else
  SNIPPET="$(mktemp)"
  SNIPPET_TMP="$SNIPPET"
  curl -fsSL "https://raw.githubusercontent.com/$GH_REPO/$BRANCH/config/claude-settings.snippet.json" -o "$SNIPPET"
fi
python3 - "$SNIPPET" <<'EOF'
import json, os, sys
snippet = json.load(open(sys.argv[1]))
p = os.path.expanduser('~/.claude/settings.json')
cur = json.load(open(p)) if os.path.exists(p) else {}
cur.setdefault('env', {}).update(snippet.get('env', {}))
for k in ('model', 'modelPicker', 'skillOverrides'):
    if k in snippet:
        cur[k] = snippet[k]
os.makedirs(os.path.dirname(p), exist_ok=True)
bak = p + '.bak-mlaude'
if os.path.exists(p):
    open(bak, 'w').write(open(p).read())
    print('    backed up settings to', bak)
json.dump(cur, open(p, 'w'), indent=2)
print('    patched ~/.claude/settings.json')
EOF

# 4b. Initial model sync (needs the OpenCode key; safe to defer).
if python3 "$ROUTER_DIR/router/sync.py" --prune; then
  echo "    initial sync done"
else
  echo "    initial sync deferred (add a key, then: mlaude sync)"
fi

# 5. Smoke test (any HTTP answer counts as up — Anthropic's root 404s,
#    banner or empty — same rule as verify.sh and mlaude.sh).
sleep 1
echo "    waiting for 127.0.0.1:$PORT ..."
up=0
for i in $(seq 1 15); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:$PORT/" || true)
  if [ -n "$code" ] && [ "$code" != "000" ]; then up=1; break; fi
  sleep 1
done
if [ "$up" = "1" ]; then echo "    router is up"; else echo "    WARNING: router not answering; check ~/Library/Logs/claude-router.log"; fi

# 6. mlaude alias: `mlaude status|start|stop|auto|help` in every new terminal.
if [ -f "$SCRIPT_DIR/mlaude.sh" ]; then
  cp "$SCRIPT_DIR/mlaude.sh" "$ROUTER_DIR/mlaude.sh"
  echo "    copied mlaude.sh from local checkout"
else
  curl -fsSL "https://raw.githubusercontent.com/$GH_REPO/$BRANCH/mlaude.sh" -o "$ROUTER_DIR/mlaude.sh"
  echo "    downloaded mlaude.sh from GitHub"
fi
chmod +x "$ROUTER_DIR/mlaude.sh"
# 6b. Daily model sync (idempotent; opt out with `mlaude auto sync off`).
bash "$ROUTER_DIR/mlaude.sh" auto sync on
ALIAS_LINE='alias mlaude='\''bash "$HOME/.local/share/claude-router/mlaude.sh"'\'
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
  touch "$rc"
  if ! grep -qF 'claude-router/mlaude.sh' "$rc"; then
    printf '\n# mlaude router control\n%s\n' "$ALIAS_LINE" >> "$rc"
    echo "    added mlaude alias to $(basename "$rc")"
  else
    echo "    mlaude alias already in $(basename "$rc")"
  fi
done
echo ""
echo "Done. Restart Claude Code, pick 'Muse Spark 1.3 (contributor)' for Meta,"
echo "'oc-*' entries for OpenCode, or any claude-* model for Anthropic."
echo "Dashboard: http://127.0.0.1:$PORT/ui (also: mlaude open)"
echo 'Full check: ./verify.sh (needs stored keys for the live probes)'
echo 'Terminal control: mlaude status|start|stop|restart|sync|keys|open|uninstall|help (restart terminal first)'
if command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT/ui" || true
fi
