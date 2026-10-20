# mlaude — one Claude session, Anthropic + Meta + OpenCode, dashboard included

*mlaude, aka **multiclaude**: the same tool, the same command — multiclaude is just the long name for using many Claudes through one localhost router.*

A tiny localhost app that lets **one Claude Code session** use three backends:

- `muse-*` → Meta (`api.meta.ai`, Responses API with `prompt_cache_key`, `/v1/messages` fallback)
- `oc-*` → OpenCode (`opencode.ai`, OpenAI-compatible chat, prefix stripped upstream)
- everything else → Anthropic (`api.anthropic.com`, byte-for-byte pass-through, streamed)

Standard library Python only (tray icon needs `pystray`+`pillow`, best-effort). A web dashboard on the same port (`/ui`) manages keys, connections, models, sync, and logs.

Standard library Python only. No Docker, no npm, no pip packages.
Verified live on macOS: `muse-spark-1.3` returns `200` with a valid Anthropic-shaped message.

## Quick install

**Mac / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/Vasiniks/mlaude/main/install.sh | bash
```

**Windows (native PowerShell — recommended, most lightweight):**

```powershell
irm https://raw.githubusercontent.com/Vasiniks/mlaude/main/install.ps1 | iex
```

**Windows (WSL2 Ubuntu — only if Claude Code runs inside WSL):** run the Mac/Linux line inside Ubuntu *instead of* the PowerShell installer. Otherwise skip this: native is lighter (no Linux VM sitting in RAM) and is the recommended setup. Never run both — two routers would fight over port 8787.

The installer will:

1. Copy `router/router.py` + `router/muse_responses.py` to `~/.local/share/claude-router/`
2. Ask for your **Meta API key** once → Keychain (`meta-model-api`) on Mac, User env var `META_API_KEY` on Windows
3. Autostart the proxy on `127.0.0.1:8787` (LaunchAgent on Mac, Scheduled Task at logon on Windows)
4. Merge `config/claude-settings.snippet.json` into `~/.claude/settings.json` (backup at `settings.json.bak-mlaude`): sets `ANTHROPIC_BASE_URL`, `MAX_CONTEXT_TOKENS=1000000`, the `muse-spark-1.3-contributor` model + picker entry, and the skill-overrides list

Then **restart Claude Code** and pick `Muse Spark 1.3 (contributor)` for Meta or any `claude-*` for Anthropic.

## mlaude command (no reinstalls)

The installer adds a persistent `mlaude` alias (new terminals; Mac: `~/.zshrc`/`~/.bashrc`, Windows: `$PROFILE`):

```bash
mlaude status       # router up? autostart? key? log tail
mlaude start        # start now
mlaude stop         # stop now (keeps login autostart)
mlaude restart      # stop, then start (picks up key changes)
mlaude sync         # fetch OpenCode models into the picker
mlaude keys         # masked key status; `keys set <svc>` stores one
mlaude open         # open the dashboard
mlaude auto         # ensure start-at-login regardless (idempotent)
mlaude auto off     # disable login start / mlaude auto status
mlaude auto sync off  # disable the daily model sync
mlaude uninstall [--keys]  # remove autostart + files (+ keys)
mlaude help
```

`ROUTER_PORT=8788 mlaude status` checks another port. Never `irm|iex` / `curl|bash` again to revive it — `mlaude start` / `mlaude auto` heals it from the installed copy in `~/.local/share/claude-router/`.

## Adding models

`add-model.py` registers any model id in the picker (stdlib only, Mac/Windows/Linux):

```bash
# add + make active
python3 add-model.py --model muse-spark-1.3-contributor \
    --label "Muse Spark 1.3 (contributor)" \
    --description "Meta, 1M context, via local router" --use

# add without switching, list entries
python3 add-model.py --model claude-opus-4-6 --label "Opus 4.6"
python3 add-model.py --list
```

Backs up settings first, never duplicates (re-running updates the entry), preserves everything else. Restart Claude Code to see new entries. Remember: only `muse-*` ids route to Meta — anything else goes to Anthropic, so the id you add decides the backend.

## OpenCode models

Models starting with `oc-` route to OpenCode (`opencode.ai`, OpenAI-compatible chat endpoint) with the `oc-` prefix stripped upstream:

```bash
mlaude keys set opencode     # store the key (prompts, never echoed)
mlaude sync                  # register free models as oc-<id>
mlaude sync --prune          # ...and drop stale oc- entries
```

`mlaude keys` shows masked presence for all three services (`meta`, `opencode`, `anthropic`). The Anthropic entry is an optional override — when empty, Anthropic traffic keeps using your Claude Code login untouched. Key changes need `mlaude restart` (the router caches keys per process).

`jev-1.13-free` is deliberately never registered: Jev is a decision endpoint, not a chat model. Hosts/paths can be overridden with `OC_HOST` / `OC_PATH` env vars.

## Dashboard

`mlaude open` (or `http://127.0.0.1:8787/ui`) — status tiles, masked key management, connection editing, model table + sync-now, log tail, and restart. Served by the router itself; same Apple-style system-type treatment as the brand page. Key changes need `mlaude restart` (keys are cached per process — the dashboard says so after every save).

## Verify

```bash
./verify.sh        # Mac/Linux
.\verify.ps1       # Windows
```

What it checks:

| # | Probe | Proves |
|---|-------|--------|
| 1 | `GET /` contains `Anthropic API` | router is up, pass-through alive |
| 2 | `claude-*` POST → `401` (no key) | Anthropic forward path works |
| 3 | `muse-spark-1.3` POST → `200` + `"type":"message"` | Meta Responses→Messages translation works |
| 4 | `stop_sequences` POST → `200` + `"type":"message"` | turns Meta can't map are sanitized, not failed |

Unit tests for the translator (no router, key, or network needed):

```bash
python3 router/test_translator.py
```

## Skills, context, and config

- **Skills are shared, not split.** The proxy never filters your tools: `merge_embedded_tools()` re-attaches tool definitions carried inside extension blocks, and unknown block types degrade to text (logged to stderr) instead of failing the turn. Both backends see the same skills/context; only the transport changes.
- **Context = 1M.** The snippet sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`. Meta turns use `prompt_cache_key=claude-code` + 24h retention so repeated prefixes bill as cached input.
- **Config files that matter:**
  - `~/.claude/settings.json` — client side (base URL, model, picker, skill overrides). The installer merges; your other keys are preserved.
  - `~/Library/LaunchAgents/com.user.claude-router.plist` (Mac) / Scheduled Task `ClaudeRouter` (Win) — autostart.
  - Keychain `meta-model-api` (Mac) / User env `META_API_KEY` (Win) — the only secret, never in the repo.
  - `config/claude-settings.snippet.json` — the exact snippet the installer merges (shipped as-is from a working machine).

## The one diff from the live machine

`router/router.py` in this repo is byte-identical to the proven Mac install **except** `meta_key()` checks `META_API_KEY` env first, then Keychain. That's what makes Windows work with zero new dependencies. Everything else (schema fixes, TTL stripping, stall/ping guards, stream translation) is untouched.

## Uninstall

`mlaude uninstall [--keys]` stops everything, removes autostart entries (including the daily sync job) and installed files. Keys survive by default; `--keys` clears Keychain entries (Mac) / User env vars (Windows) / `~/.config/mlaude` (Linux).

Manual equivalent (if the CLI is gone):

**Mac:**

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.user.claude-router.plist 2>/dev/null || launchctl unload ~/Library/LaunchAgents/com.user.claude-router.plist
rm ~/Library/LaunchAgents/com.user.claude-router.plist
rm -rf ~/.local/share/claude-router
# restore settings: cp ~/.claude/settings.json.bak-mlaude ~/.claude/settings.json
```

**Windows:**

```powershell
Unregister-ScheduledTask -TaskName ClaudeRouter -Confirm:$false
Remove-Item -Recurse "$env:USERPROFILE\.local\share\claude-router"
[Environment]::SetEnvironmentVariable('META_API_KEY', $null, 'User')
```

## Troubleshooting

- `Meta API key not found (502)` → re-run the installer and paste the key, or set `META_API_KEY` env and restart the router.
- Meta answers `invalid_api_key / Unauthorized` → the stored key is wrong. Re-running the installer heals whitespace-mangled pastes automatically; if it still fails, the key itself is bad — clear it and paste a fresh one from https://developers.meta.ai, then re-run:
  - Mac: `security delete-generic-password -a "$USER" -s meta-model-api`
  - Windows: `[Environment]::SetEnvironmentVariable('META_API_KEY', $null, 'User')` (also `$env:META_API_KEY = $null` in the current window)
- Port in use → `ROUTER_PORT=8788 ./install.sh` (and update `ANTHROPIC_BASE_URL` to match).
- `muse via messages (fallback: …)` in logs → the translator hit something unmappable and fell back to Meta `/v1/messages`. Normal.
- `muse: dropped stop_sequences / server tool ...` in logs → the router sanitized a turn Meta can't serve (drops the param/tool, serves via Responses). Degraded but working, by design — not an error.
- Meta answers `401 invalid x-api-key` on a fallback turn while normal turns return `200` → the key is Responses-scoped; the router keeps turns on the Responses path automatically. Don't rotate the key for this.
- `prompt is too long` → Claude Code compacts and retries automatically (the translator maps Meta's length errors to that string).
- Router log: Mac `~/Library/Logs/claude-router.log`, Windows `%USERPROFILE%\.local\share\claude-router\router.log`.
- Re-running an installer is safe (idempotent). On Windows, stop any manually-started router (`Ctrl+C`) first so the smoke test validates the scheduled task instead of your window.

## Security

- Binds `127.0.0.1` only — never expose it to a network.
- No secrets in this repo. The key lives in Keychain / User env, the settings backup is local-only.
- Public repo safe: `router/`, installers, plist/XML template, config snippet, and docs contain no tokens.

## Layout

```
mlaude/
  router/router.py  router/muse_responses.py   # Meta lane (stdlib only)
  router/oc_chat.py                             # OpenCode lane translator
  router/keys.py  router/connections.py         # key store + endpoint settings
  router/sync.py                                # model-list picker sync
  router/api.py  router/ui.html                 # dashboard backend + page
  router/test_*.py                              # offline unit tests, no key/network
  app/main.py  app/tray.py                      # unified app + best-effort tray
  install.sh  install.ps1                       # installers (curl | bash / irm | iex)
  mlaude.sh  mlaude.ps1                         # terminal control (status/start/…/uninstall)
  verify.sh   verify.ps1                        # live probes above (oc- probe skips without a key)
  add-model.py                                  # register models in the picker anytime
  launchagents/com.user.claude-router*.plist    # Mac autostart + daily sync templates
  windows/claude-router*.xml                    # Win Scheduled Task templates
  config/claude-settings.snippet.json           # Claude settings merged by installer
```

MIT — see `LICENSE`.
