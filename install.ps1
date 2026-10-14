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
  Copy-Item (Join-Path $here 'router\router.py') (Join-Path $RouterDir 'router.py') -Force
  Copy-Item (Join-Path $here 'router\muse_responses.py') (Join-Path $RouterDir 'muse_responses.py') -Force
  Write-Host '    copied router files from local checkout'
} else {
  $base = "https://raw.githubusercontent.com/$GH_REPO/$Branch/router"
  Invoke-WebRequest "$base/router.py" -OutFile (Join-Path $RouterDir 'router.py')
  Invoke-WebRequest "$base/muse_responses.py" -OutFile (Join-Path $RouterDir 'muse_responses.py')
  Write-Host '    downloaded router files from GitHub'
}
$pyBin = ($PyCmd -split ' ')[0]; $pyArgs = ($PyCmd -split ' ')[1..9]
& $pyBin @pyArgs -m py_compile (Join-Path $RouterDir 'router.py') (Join-Path $RouterDir 'muse_responses.py')
Write-Host '    compile OK'

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

# 3. Scheduled task: start router at logon (no admin needed).
# A wrapper script gives us a log file (like the Mac plist) and a fixed
# working directory; the task runs it hidden under an interactive principal.
$wrapper = Join-Path $RouterDir 'start-router.ps1'
@"
& "$PyExe" "$RouterDir\router.py" >> "$RouterDir\router.log" 2>&1
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

# 5. Smoke test (root answers 404 + banner, so any HTTP answer counts as up).
Start-Sleep 2
$up = $false
for ($i = 0; $i -lt 15 -and -not $up; $i++) {
  try {
    Invoke-WebRequest "http://127.0.0.1:$Port/" -TimeoutSec 3 -SkipHttpErrorCheck | Out-Null
    $up = $true
  } catch { Start-Sleep 1 }
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
Write-Host "Done. Restart Claude Code, pick 'Muse Spark 1.3 (contributor)' for Meta"
Write-Host 'or any claude-* model for Anthropic. Full check: .\verify.ps1'
Write-Host 'Terminal control: mlaude status|start|stop|auto|help (restart terminal first)'
Write-Host "WSL alternative: run install.sh inside Ubuntu with the same repo."
