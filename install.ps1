# mlaude installer — Windows native (PowerShell 5.1+).
# Run from an elevated-or-normal PowerShell (normal is fine):
#   irm https://raw.githubusercontent.com/Vasiniks/mlaude/main/install.ps1 | iex
# Local:
#   .\install.ps1
$ErrorActionPreference = 'Stop'
$GH_REPO = if ($env:GH_REPO) { $env:GH_REPO } else { 'Vasiniks/mlaude' }
$Branch  = if ($env:BRANCH) { $env:BRANCH } else { 'main' }
$Port    = if ($env:ROUTER_PORT) { $env:ROUTER_PORT } else { '8787' }
$RouterDir = Join-Path $env:USERPROFILE '.local\share\claude-router'
$TaskName  = 'ClaudeRouter'
$KeyTarget = 'meta-model-api'

Write-Host "==> mlaude: installing router to $RouterDir (port $Port)"
New-Item -ItemType Directory -Force -Path $RouterDir | Out-Null

function Get-Python {
  foreach ($c in @('py -3', 'python', 'python3')) {
    $bin = ($c -split ' ')[0]; $args = ($c -split ' ')[1..9]
    try {
      & $bin @args --version 2>$null | Out-Null
      if ($LASTEXITCODE -eq 0) { return $c }
    } catch {}
  }
  throw 'Python 3 not found. Install from https://www.python.org/downloads/ (tick "Add to PATH"), then re-run.'
}
$PyCmd = Get-Python
Write-Host "    python: $PyCmd"

# 1. Router files: local copy when run from a clone, else download.
# $MyInvocation.MyCommand.Path is empty when run via `irm ... | iex`
# (no script file); fall back to download mode instead of crashing.
$here = if ($MyInvocation.MyCommand.Path) { Split-Path -Parent $MyInvocation.MyCommand.Path } else { $null }
if (($here) -and (Test-Path (Join-Path $here 'router\router.py'))) {
  New-Item -ItemType Directory -Force -Path (Join-Path $RouterDir 'router') | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $RouterDir 'app') | Out-Null
  $routerFiles = @('router.py', 'muse_responses.py', 'oc_chat.py', 'keys.py', 'sync.py', 'api.py', 'connections.py', 'ui.html', 'test_router.py', 'test_keys.py', 'test_sync.py', 'test_api.py', 'test_oc_chat.py', 'test_translator.py', 'test_connections.py')
  $appFiles = @('main.py', 'tray.py', 'test_app.py')
  foreach ($f in $routerFiles) { Copy-Item (Join-Path $here "router\$f") (Join-Path $RouterDir "router\$f") -Force }
  foreach ($f in $appFiles) { Copy-Item (Join-Path $here "app\$f") (Join-Path $RouterDir "app\$f") -Force }
  Write-Host '    copied router + app files from local checkout'
} else {
  $base = "https://raw.githubusercontent.com/$GH_REPO/$Branch"
  New-Item -ItemType Directory -Force -Path (Join-Path $RouterDir 'router') | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $RouterDir 'app') | Out-Null
  $routerFiles = @('router.py', 'muse_responses.py', 'oc_chat.py', 'oc_local.py', 'keys.py', 'sync.py', 'api.py', 'connections.py', 'ui.html')
  $appFiles = @('main.py', 'tray.py')
  foreach ($f in $routerFiles) { Invoke-WebRequest "$base/router/$f" -OutFile (Join-Path $RouterDir "router\$f") }
  foreach ($f in $appFiles) { Invoke-WebRequest "$base/app/$f" -OutFile (Join-Path $RouterDir "app\$f") }
  Write-Host '    downloaded router + app files from GitHub'
}
$pyBin = ($PyCmd -split ' ')[0]; $pyArgs = ($PyCmd -split ' ')[1..9]
& $pyBin @pyArgs -m py_compile (Join-Path $RouterDir 'router\router.py') (Join-Path $RouterDir 'router\keys.py') (Join-Path $RouterDir 'router\sync.py') (Join-Path $RouterDir 'router\api.py') (Join-Path $RouterDir 'router\oc_chat.py') (Join-Path $RouterDir 'router\oc_local.py') (Join-Path $RouterDir 'app\main.py')
Write-Host '    compile OK'

# Tray deps (best-effort): without them the app runs headless + dashboard.
try {
  & $pyBin @pyArgs -m pip install --user pystray pillow | Out-Null
  Write-Host '    tray deps installed'
} catch {
  Write-Host '    tray deps skipped (headless mode; dashboard still works)'
}

# Resolve full python exe for the scheduled task.
$PyExe = (& $pyBin @pyArgs -c 'import sys; print(sys.executable)')
Write-Host "    python exe: $PyExe"

# 2. Meta API key -> User environment variable META_API_KEY (registry, survives reboots).
#    The router reads $env:META_API_KEY first, Keychain only on macOS.
#    API tokens never contain whitespace, so strip it all: heals broken pastes
#    (stray newline/space) instead of storing a key Meta will reject.
$candidate = if ($env:META_API_KEY) { $env:META_API_KEY } else { [Environment]::GetEnvironmentVariable('META_API_KEY', 'User') }
$candidate = if ($candidate) { $candidate -replace '\s', '' } else { '' }
if (-not $candidate) {
  $sec = Read-Host '    Paste your Meta API key (https://developers.meta.ai)' -AsSecureString
  $candidate = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)) -replace '\s', ''
  if (-not $candidate) { throw 'empty key; aborting' }
}
$stored = [Environment]::GetEnvironmentVariable('META_API_KEY', 'User')
if ($candidate -ne $stored) {
  [Environment]::SetEnvironmentVariable('META_API_KEY', $candidate, 'User')
  Write-Host '    stored META_API_KEY in User env'
} else {
  Write-Host '    META_API_KEY already in User env; leaving it alone'
}
$env:META_API_KEY = $candidate

# 2b. OpenCode key (skippable — set later in the dashboard or via
# `mlaude keys set opencode`).
$ocCandidate = if ($env:OPENCODE_API_KEY) { $env:OPENCODE_API_KEY } else { [Environment]::GetEnvironmentVariable('OPENCODE_API_KEY', 'User') }
$ocCandidate = if ($ocCandidate) { $ocCandidate -replace '\s', '' } else { '' }
if (-not $ocCandidate) {
  $plain = Read-Host '    Paste your OpenCode API key (Enter to skip, dashboard later)'
  $ocCandidate = $plain -replace '\s', ''
}
if (-not $ocCandidate) {
  Write-Host '    skipped (dashboard or `mlaude keys set opencode` later)'
} else {
  $ocStored = [Environment]::GetEnvironmentVariable('OPENCODE_API_KEY', 'User')
  if ($ocCandidate -ne $ocStored) {
    [Environment]::SetEnvironmentVariable('OPENCODE_API_KEY', $ocCandidate, 'User')
    Write-Host '    stored OPENCODE_API_KEY in User env'
  } else {
    Write-Host '    OPENCODE_API_KEY already in User env; leaving it alone'
  }
  $env:OPENCODE_API_KEY = $ocCandidate
}

# 3. Scheduled task: start router at logon (no admin needed).
# A wrapper script gives us a log file (like the Mac plist) and a fixed
# working directory; the task runs it hidden under an interactive principal.
$wrapper = Join-Path $RouterDir 'start-router.ps1'
# Pin the install-time port (like the Mac plist's ROUTER_PORT) so logon
# starts honor custom ports instead of silently reverting to 8787.
# The unified app (proxy child + tray) is the entrypoint, not router.py.
@"
`$env:ROUTER_PORT = "$Port"
& "$PyExe" "$RouterDir\app\main.py" >> "$RouterDir\router.log" 2>&1
"@ | Set-Content $wrapper -Encoding utf8
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -WindowStyle Hidden -File `"$wrapper`"" -WorkingDirectory $RouterDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  # Restart, not just start: the router caches the API key per process, so an
  # already-running instance would keep serving the old key after a key change.
  try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
  Start-Sleep 1
  Start-ScheduledTask -TaskName $TaskName
  Write-Host '    scheduled task registered + started (log: router.log)'
} catch {
  Write-Warning "    could not register scheduled task: $_"
  Write-Host "    fallback: run in a window with: $PyExe `"$RouterDir\router.py`""
}

# 4. Claude settings merge (preserve everything else; backup first).
$snippetLocal = if ($here) { Join-Path $here 'config\claude-settings.snippet.json' } else { $null }
$snippetPath = if (($snippetLocal) -and (Test-Path $snippetLocal)) { $snippetLocal } else {
  $tmp = Join-Path $env:TEMP 'mlaude-snippet.json'
  Invoke-WebRequest "https://raw.githubusercontent.com/$GH_REPO/$Branch/config/claude-settings.snippet.json" -OutFile $tmp
  $tmp
}
$mergeScript = @'
import json, os, sys
snippet = json.load(open(sys.argv[1]))
home = os.path.expanduser('~')
p = os.path.join(home, '.claude', 'settings.json')
cur = json.load(open(p)) if os.path.exists(p) else {}
cur.setdefault('env', {}).update(snippet.get('env', {}))
for k in ('model', 'modelPicker', 'skillOverrides'):
    if k in snippet:
        cur[k] = snippet[k]
os.makedirs(os.path.dirname(p), exist_ok=True)
if os.path.exists(p):
    open(p + '.bak-mlaude', 'w').write(open(p).read())
    print('    backed up settings')
json.dump(cur, open(p, 'w'), indent=2)
print('    patched ~/.claude/settings.json')
'@
# pipe the script to stdin: `python -` reads the program from stdin,
# so passing it as an argument drops into an interactive REPL instead.
$mergeScript | & $pyBin @pyArgs - $snippetPath

# 5. Smoke test (no -SkipHttpErrorCheck: it doesn't exist on Windows
#    PowerShell 5.1. Any HTTP answer counts as up, so catch the 404 throw
#    and treat a response object as alive; only a connection failure waits).
Start-Sleep 2
$up = $false
for ($i = 0; $i -lt 15 -and -not $up; $i++) {
  try {
    Invoke-WebRequest "http://127.0.0.1:$Port/" -TimeoutSec 3 -UseBasicParsing | Out-Null
    $up = $true
  } catch {
    if ($_.Exception.Response) { $up = $true } else { Start-Sleep 1 }
  }
}
if ($up) { Write-Host '    router is up' }
else { Write-Warning "    router not answering; see $RouterDir\router.log, or run .\verify.ps1" }

# 6. mlaude alias: `mlaude status|start|stop|auto|help` in every new terminal.
$mlaudeLocal = if ($here) { Join-Path $here 'mlaude.ps1' } else { $null }
$mlaudeDest = Join-Path $RouterDir 'mlaude.ps1'
if (($mlaudeLocal) -and (Test-Path $mlaudeLocal)) {
  Copy-Item $mlaudeLocal $mlaudeDest -Force
  Write-Host '    copied mlaude.ps1 from local checkout'
} else {
  Invoke-WebRequest "https://raw.githubusercontent.com/$GH_REPO/$Branch/mlaude.ps1" -OutFile $mlaudeDest
  Write-Host '    downloaded mlaude.ps1 from GitHub'
}
# Persist: dot-source wrapper in $PROFILE (idempotent) + define now.
$wrapperLine = 'function mlaude { & "$env:USERPROFILE\.local\share\claude-router\mlaude.ps1" @args }'
try {
  if (-not (Test-Path $PROFILE)) { New-Item -ItemType File -Force -Path $PROFILE | Out-Null }
  $prof = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
  if ($prof -notmatch 'claude-router\\mlaude\.ps1') {
    Add-Content $PROFILE "`n# mlaude router control`n$wrapperLine"
    Write-Host '    added mlaude function to $PROFILE'
  } else {
    Write-Host '    mlaude function already in $PROFILE'
  }
} catch {
  Write-Warning "    could not update `$PROFILE: $_ (run manually once: $wrapperLine)"
}
try { Invoke-Expression $wrapperLine } catch {}
Write-Host ''

# Initial model sync + daily sync job (both tolerate a missing key).
try {
  & $pyBin @pyArgs (Join-Path $RouterDir 'router\sync.py') --prune
  Write-Host '    initial sync done'
} catch {
  Write-Host '    initial sync deferred (add a key, then: mlaude sync)'
}
try { & $mlaudeDest auto sync on } catch { Write-Warning "    daily sync not scheduled: $_" }

Write-Host "Done. Restart Claude Code, pick 'Muse Spark 1.3 (contributor)' for Meta,"
Write-Host "'oc-*' entries for OpenCode, or any claude-* model for Anthropic."
Write-Host "Dashboard: http://127.0.0.1:$Port/ui (also: mlaude open)"
Write-Host 'Full check: .\verify.ps1 (needs stored keys for the live probes)'
Write-Host 'Terminal control: mlaude status|start|stop|restart|sync|keys|open|uninstall|help (restart terminal first)'
try { Start-Process "http://127.0.0.1:$Port/ui" } catch {}
Write-Host "WSL alternative: run install.sh inside Ubuntu with the same repo."
