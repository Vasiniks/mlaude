# mlaude — one Claude session, Anthropic + Meta models, 1M context

A tiny localhost proxy that lets **one Claude Code session** use both providers:

- `muse-*` → Meta (`api.meta.ai`, Responses API with `prompt_cache_key`, `/v1/messages` fallback)
- everything else → Anthropic (`api.anthropic.com`, byte-for-byte pass-through, streamed)

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
mlaude auto         # ensure start-at-login regardless (idempotent)
mlaude auto off     # disable login start / mlaude auto status
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
- `muse via messages (fallback: …)` in logs → the translator hit something unmappable and correctly fell back to Meta `/v1/messages`. Normal.
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
  router/router.py  router/muse_responses.py   # the proxy (stdlib only)
  install.sh  install.ps1                       # installers (curl | bash / irm | iex)
  mlaude.sh  mlaude.ps1                         # terminal alias (status/start/stop/auto/help)
  verify.sh   verify.ps1                        # the 3 probes above
  launchagents/com.user.claude-router.plist     # Mac autostart template
  windows/claude-router-task.xml                # Win Scheduled Task template
  config/claude-settings.snippet.json           # Claude settings merged by installer
```

MIT — see `LICENSE`.
