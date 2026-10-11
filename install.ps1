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
$existing = [Environment]::GetEnvironmentVariable('META_API_KEY', 'User')
if ($env:META_API_KEY) {
  [Environment]::SetEnvironmentVariable('META_API_KEY', $env:META_API_KEY, 'User')
  Write-Host '    stored META_API_KEY from current session into User env'
} elseif ($existing) {
  Write-Host '    META_API_KEY already in User env; leaving it alone'
} else {
  $sec = Read-Host '    Paste your Meta API key (https://developers.meta.ai)' -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  if (-not $plain) { throw 'empty key; aborting' }
  [Environment]::SetEnvironmentVariable('META_API_KEY', $plain, 'User')
  $env:META_API_KEY = $plain
  Write-Host '    stored META_API_KEY in User env'
}

# 3. Scheduled task: start router at logon (no admin needed).
$action   = New-ScheduledTaskAction -Execute $PyExe -Argument "`"$RouterDir\router.py`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  Write-Host '    scheduled task registered + started'
} catch {
  Write-Warning "    could not register scheduled task: $_"
  Write-Host "    fallback: run manually with: $PyExe `"$RouterDir\router.py`""
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

# 5. Smoke test.
Start-Sleep 2
try {
  (Invoke-WebRequest "http://127.0.0.1:$Port/" -TimeoutSec 5).StatusCode | Out-Null
  Write-Host '    router is up'
} catch { Write-Warning "    router not answering yet; it starts at logon, or run verify.ps1" }
Write-Host ''
Write-Host "Done. Restart Claude Code, pick 'Muse Spark 1.3 (contributor)' for Meta"
Write-Host 'or any claude-* model for Anthropic. Full check: .\verify.ps1'
Write-Host "WSL alternative: run install.sh inside Ubuntu with the same repo."
