#!/usr/bin/env bash
# mlaude — terminal alias for the claude-router (Mac/Linux).
# Installed by install.sh to ~/.local/share/claude-router/mlaude.sh
# with an alias appended to ~/.zshrc / ~/.bashrc:
#   alias mlaude='bash "$HOME/.local/share/claude-router/mlaude.sh"'
# Usage: mlaude {status|start|stop|auto|help} [port|on|off]
set -uo pipefail

PORT="${ROUTER_PORT:-8787}"
ROUTER_DIR="${ROUTER_DIR:-$HOME/.local/share/claude-router}"
ROUTER_APP="$ROUTER_DIR/app/main.py"
ROUTER_PY="$ROUTER_DIR/router/router.py"
SYNC_PY="$ROUTER_DIR/router/sync.py"
PLIST="$HOME/Library/LaunchAgents/com.user.claude-router.plist"
SYNC_PLIST="$HOME/Library/LaunchAgents/com.user.claude-router-sync.plist"
LABEL="com.user.claude-router"
LOG_MAC="$HOME/Library/Logs/claude-router.log"
LOG_FILE="$ROUTER_DIR/router.log"

usage() {
  cat <<'EOF'
mlaude — claude-router control (Mac/Linux)

  mlaude status [port]   router up? autostart on? key present? log tail
  mlaude start  [port]   start now (LaunchAgent on Mac, nohup fallback)
  mlaude stop            stop now (keeps autostart file; use `auto off` to disable login start)
  mlaude restart [port]  stop, then start (picks up key changes)
  mlaude sync [--prune]  fetch OpenCode free models into the picker
  mlaude keys [list|set SVC]  show (masked) or store API keys (SVC: meta|opencode|anthropic)
  mlaude open [port]     open the dashboard in a browser
  mlaude uninstall [--keys]  stop, remove autostart + files (+ keys with --keys)
  mlaude auto [on|off|status]  no arg = on: ensure start-at-login regardless (idempotent)
  mlaude auto sync [on|off|status]  daily model sync job
  mlaude help            this text

Env: ROUTER_PORT=8788 mlaude status   ROUTER_DIR=... mlaude start
Logs: ~/Library/Logs/claude-router.log (Mac) | ~/.local/share/claude-router/router.log (fallback)
EOF
}

is_mac() { [ "$(uname -s)" = "Darwin" ]; }

# Any HTTP answer (even Anthropic's root 404) counts as up.
router_up() {
  local port="${1:-$PORT}"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 "http://127.0.0.1:$port/" 2>/dev/null || true)
  [ -n "$code" ] && [ "$code" != "000" ]
}

router_pid() {
  # Matches both v1 flat installs (.../claude-router/router.py) and v2
  # mirrored installs (.../claude-router/router/router.py).
  pgrep -f "router/router.py" 2>/dev/null || pgrep -f "router\.py" 2>/dev/null || true
}

auto_state() {
  if is_mac; then
    if [ ! -f "$PLIST" ]; then echo "off (no plist)"; return; fi
    if launchctl list 2>/dev/null | grep -q "$LABEL"; then echo "on (loaded)"; else echo "off (plist present, not loaded)"; fi
  else
    if crontab -l 2>/dev/null | grep -q "app/main.py"; then echo "on (cron @reboot)"; else echo "off (no cron @reboot)"; fi
  fi
}

key_present() {
  if [ -n "${META_API_KEY:-}" ]; then echo "env META_API_KEY"; return; fi
  if is_mac && security find-generic-password -a "$USER" -s meta-model-api -w >/dev/null 2>&1; then
    echo "Keychain (meta-model-api)"; return
  fi
  echo "MISSING"
}

cmd_status() {
  local port="${1:-$PORT}"
  echo "==> mlaude status (port $port)"
  if router_up "$port"; then echo "  router: UP  (http://127.0.0.1:$port/)"; else echo "  router: DOWN (nothing on http://127.0.0.1:$port/)"; fi
  local pids
  pids=$(router_pid)
  if [ -n "$pids" ]; then echo "  process: running (pid $(echo "$pids" | tr '\n' ' '))"; else echo "  process: not running"; fi
  echo "  autostart: $(auto_state)"
  echo "  key: $(key_present)"
  local log=""
  if [ -f "$LOG_MAC" ]; then log="$LOG_MAC"; elif [ -f "$LOG_FILE" ]; then log="$LOG_FILE"; fi
  if [ -n "$log" ]; then echo "  log ($log, last 5):"; tail -n 5 "$log" 2>/dev/null | sed 's/^/    /'; else echo "  log: none yet"; fi
}

cmd_start() {
  local port="${1:-$PORT}"
  if router_up "$port"; then echo "router already up on $port"; return 0; fi
  echo "starting router (port $port)..."
  if is_mac && [ -f "$PLIST" ]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
  fi
  for _ in $(seq 1 8); do router_up "$port" && { echo "router is up"; return 0; }; sleep 1; done
  # Fallback: direct background start (also the Linux path).
  local py
  py="$(command -v python3)"
  [ -n "$py" ] || { echo "python3 not found" >&2; return 1; }
  mkdir -p "$ROUTER_DIR"
  echo "  LaunchAgent didn't answer; starting in background (log: $LOG_FILE)"
  ROUTER_PORT="$port" nohup "$py" "$ROUTER_APP" --no-tray >>"$LOG_FILE" 2>&1 &
  for _ in $(seq 1 8); do router_up "$port" && { echo "router is up"; return 0; }; sleep 1; done
  echo "WARNING: router not answering; check $LOG_MAC and $LOG_FILE" >&2
  return 1
}

cmd_stop() {
  echo "stopping router (keeps autostart file)..."
  if is_mac; then
    launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
  fi
  local pids
  pids=$(router_pid)
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
  fi
  if router_up "$PORT"; then echo "still answering on $PORT (a copy may be running elsewhere)"; return 1; fi
  echo "stopped"
}

cmd_restart() {
  cmd_stop && cmd_start "${1:-$PORT}"
}

_router_py() {
  local name="$1"; shift
  local py
  py="$(command -v python3)"
  [ -n "$py" ] || { echo "python3 not found" >&2; return 1; }
  if [ ! -f "$ROUTER_DIR/router/$name" ]; then
    echo "$name not installed in $ROUTER_DIR/router (re-run the installer)" >&2
    return 1
  fi
  "$py" "$ROUTER_DIR/router/$name" "$@"
}

cmd_sync() {
  _router_py sync.py "$@" || return 1
}

cmd_keys() {
  if [ "$#" -eq 0 ]; then
    _router_py keys.py list
  else
    _router_py keys.py "$@"
  fi
}

cmd_open() {
  local port="${1:-$PORT}"
  local url="http://127.0.0.1:$port/ui"
  if command -v open >/dev/null 2>&1; then open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"
  else python3 -c "import webbrowser,sys; sys.exit(0 if webbrowser.open('$url') else 1)" \
    || echo "$url"
  fi
}

cmd_uninstall() {
  local with_keys="${1:-}"
  echo "uninstalling mlaude..."
  cmd_stop >/dev/null 2>&1 || true
  if is_mac; then
    for plist in "$PLIST" "$SYNC_PLIST"; do
      launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || launchctl unload "$plist" >/dev/null 2>&1 || true
      rm -f "$plist" && echo "  removed $(basename "$plist")"
    done
  else
    local cur=""
    cur=$(crontab -l 2>/dev/null | grep -v "claude-router/" || true)
    printf "%s\n" "$cur" | crontab - && echo "  removed cron jobs"
  fi
  rm -rf "$ROUTER_DIR" && echo "  removed $ROUTER_DIR"
  if [ "$with_keys" = "--keys" ]; then
    if is_mac; then
      for svc in meta-model-api opencode-api anthropic-api; do
        security delete-generic-password -a "$USER" -s "$svc" >/dev/null 2>&1 &&
          echo "  removed Keychain $svc" || echo "  Keychain $svc: already gone"
      done
    else
      rm -rf "$HOME/.config/mlaude" && echo "  removed ~/.config/mlaude"
    fi
  else
    echo "  keys kept (re-run with --keys to remove them too)"
  fi
  echo "  manual cleanup: the mlaude alias in ~/.zshrc / ~/.bashrc"
}

sync_state() {
  if is_mac; then
    if launchctl list 2>/dev/null | grep -q "claude-router-sync"; then echo "on (daily)"; else echo "off"; fi
  else
    if crontab -l 2>/dev/null | grep -q "sync.py"; then echo "on (daily)"; else echo "off"; fi
  fi
}

# auto with no arg = on: ensure start-at-login regardless (idempotent).
cmd_auto() {
  if [ "${1:-}" = "sync" ]; then cmd_auto_sync "${2:-status}"; return; fi
  local arg="${1:-on}"
  case "$arg" in
    on)
      if is_mac; then
        local py
        py="$(command -v python3)"
        mkdir -p "$(dirname "$PLIST")" "$ROUTER_DIR"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$ROUTER_APP</string>
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
        launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
        echo "autostart: on (LaunchAgent $LABEL loaded, starts at login)"
      else
        local py
        py="$(command -v python3)"
        mkdir -p "$ROUTER_DIR"
        local line="@reboot ROUTER_PORT=$PORT $py $ROUTER_APP --no-tray >>$LOG_FILE 2>&1"
        local cur=""
        cur=$(crontab -l 2>/dev/null | grep -v "claude-router/\(router\|app\)/\(router\|main\)\.py" || true)
        (printf "%s\n%s\n" "$cur" "$line") | crontab -
        echo "autostart: on (cron @reboot installed)"
      fi
      ;;
    off)
      if is_mac; then
        launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
        rm -f "$PLIST"
        echo "autostart: off (plist removed; router stopped)"
      else
        local cur=""
        cur=$(crontab -l 2>/dev/null | grep -v "app/main.py" || true)
        printf "%s\n" "$cur" | crontab -
        echo "autostart: off (cron @reboot removed)"
      fi
      ;;
    status)
      echo "autostart: $(auto_state)"
      ;;
    *)
      echo "unknown auto arg: $arg (use on|off|status)" >&2; return 1 ;;
  esac
}

cmd_auto_sync() {
  local arg="${1:-status}"
  case "$arg" in
    on)
      if is_mac; then
        local py
        py="$(command -v python3)"
        mkdir -p "$(dirname "$SYNC_PLIST")"
        cat > "$SYNC_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.claude-router-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$SYNC_PY</string>
    <string>--prune</string>
  </array>
  <key>StartInterval</key><integer>86400</integer>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/claude-router-sync.log</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/claude-router-sync.log</string>
</dict>
</plist>
EOF
        launchctl bootout "gui/$(id -u)" "$SYNC_PLIST" >/dev/null 2>&1 || launchctl unload "$SYNC_PLIST" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$SYNC_PLIST" 2>/dev/null || launchctl load "$SYNC_PLIST"
        echo "daily sync: on"
      else
        local py
        py="$(command -v python3)"
        local line="@daily ROUTER_PORT=$PORT $py $SYNC_PY --prune >>$LOG_FILE 2>&1"
        local cur=""
        cur=$(crontab -l 2>/dev/null | grep -v "sync.py" || true)
        (printf "%s\n%s\n" "$cur" "$line") | crontab -
        echo "daily sync: on (cron @daily)"
      fi
      ;;
    off)
      if is_mac; then
        launchctl bootout "gui/$(id -u)" "$SYNC_PLIST" >/dev/null 2>&1 || launchctl unload "$SYNC_PLIST" >/dev/null 2>&1 || true
        rm -f "$SYNC_PLIST"
      else
        local cur=""
        cur=$(crontab -l 2>/dev/null | grep -v "sync.py" || true)
        printf "%s\n" "$cur" | crontab -
      fi
      echo "daily sync: off"
      ;;
    status)
      echo "daily sync: $(sync_state)"
      ;;
    *)
      echo "unknown auto sync arg: $arg (use on|off|status)" >&2; return 1 ;;
  esac
}

cmd="${1:-help}"
arg="${2:-}"
case "$cmd" in
  status) cmd_status "${arg:-$PORT}" ;;
  start)  cmd_start "${arg:-$PORT}" ;;
  stop)   cmd_stop ;;
  restart) cmd_restart "${arg:-$PORT}" ;;
  sync)   shift; cmd_sync "$@" ;;
  keys)   shift; cmd_keys "$@" ;;
  open)   cmd_open "${arg:-$PORT}" ;;
  uninstall) cmd_uninstall "$arg" ;;
  auto)   shift; cmd_auto "$@" ;;
  help|-h|--help) usage ;;
  *) echo "unknown command: $cmd" >&2; usage; exit 1 ;;
esac
