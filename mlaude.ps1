# mlaude -- terminal alias for the claude-router (Windows PowerShell 5.1+).
# Installed by install.ps1 to $env:USERPROFILE\.local\share\claude-router\mlaude.ps1
# with a wrapper in $PROFILE:
#   function mlaude { & "$env:USERPROFILE\.local\share\claude-router\mlaude.ps1" @args }
# Usage: mlaude {status|start|stop|auto|help} [port|on|off]
param(
  [Parameter(Position = 0)][string]$Cmd = 'help',
  [Parameter(Position = 1)][string]$Arg = '',
  [string]$Port = ''
)
$ErrorActionPreference = 'Stop'
$TaskName = 'ClaudeRouter'
$SyncTaskName = 'ClaudeRouterSync'
$RouterDir = Join-Path $env:USERPROFILE '.local\share\claude-router'
$AppPy = Join-Path $RouterDir 'app\main.py'
$SyncPy = Join-Path $RouterDir 'router\sync.py'
$KeysPy = Join-Path $RouterDir 'router\keys.py'
$Wrapper = Join-Path $RouterDir 'start-router.ps1'
$SyncWrapper = Join-Path $RouterDir 'start-sync.ps1'
$LogFile = Join-Path $RouterDir 'router.log'
$SyncLog = Join-Path $RouterDir 'sync.log'
$Port = if ($Port) { $Port } elseif ($env:ROUTER_PORT) { $env:ROUTER_PORT } else { '8787' }
$Rest = @($args)
# `mlaude status 8788` treats the extra arg as a port.
if (($Cmd -eq 'status' -or $Cmd -eq 'start' -or $Cmd -eq 'restart' -or $Cmd -eq 'open') -and ($Arg -match '^\d+$')) { $Port = $Arg; $Arg = '' }

function Show-Help {
  @'
mlaude -- claude-router control (Windows)

  mlaude status [port]   router up? task state? key present? log tail
  mlaude start  [port]   start now (Scheduled Task, fallback: python window)
  mlaude stop            stop now (keeps task; use `mlaude auto off` to disable login start)
  mlaude restart [port]  stop, then start (picks up key changes)
  mlaude sync [--prune]  fetch OpenCode free models into the picker
  mlaude keys [list|set SVC]  show (masked) or store API keys (SVC: meta|opencode|anthropic)
  mlaude open [port]     open the dashboard in a browser
  mlaude uninstall [--keys]  stop, remove tasks + files (+ keys with --keys)
  mlaude auto [on|off|status]  no arg = on: ensure start-at-login regardless (idempotent)
  mlaude auto sync [on|off|status]  daily model sync job
  mlaude help            this text

Env: $env:ROUTER_PORT=8788; mlaude status
Log: %USERPROFILE%\.local\share\claude-router\router.log
'@
}

# Any HTTP answer (even Anthropic's root 404) counts as up. Works on 5.1 + 7+.
function Test-RouterUp([string]$P) {
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$P/" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    return $true
  } catch {
    $msg = "$_"
    if ($msg -match '404|Anthropic|InternalServerError|Unauthorized|BadRequest|OK') { return $true }
    if ($_.Exception.Response) {
      try { if ([int]$_.Exception.Response.StatusCode) { return $true } } catch {}
      return $true  # got an HTTP response of any kind
    }
    return $false
  }
}

function Get-RouterProcess {
  try {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop |
      Where-Object { $_.CommandLine -match 'router\.py' }
  } catch { @() }
}

function Get-TaskState {
  try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($t.State -eq 'Disabled') { return 'off (Disabled - will NOT start at login)' }
    if ($t.State -eq 'Running') { return 'on (Running now, starts at login)' }
    return "on ($($t.State) - starts at login)"
  } catch { return 'missing (run mlaude auto to create)' }
}

function Get-KeyState {
  $u = [Environment]::GetEnvironmentVariable('META_API_KEY', 'User')
  if ($u) { return 'User env META_API_KEY' }
  if ($env:META_API_KEY) { return 'process env META_API_KEY (not persisted)' }
  return 'MISSING'
}

function Invoke-Status([string]$P) {
  Write-Host "==> mlaude status (port $P)"
  if (Test-RouterUp $P) { Write-Host '  router: UP  (http://127.0.0.1:'"$P"'/)' } else { Write-Host '  router: DOWN (nothing on http://127.0.0.1:'"$P"'/)' }
  $procs = @(Get-RouterProcess)
  if ($procs.Count -gt 0) { Write-Host ("  process: running (pid " + (($procs | ForEach-Object { $_.ProcessId }) -join ' ') + ")") }
  else { Write-Host '  process: not running' }
  Write-Host ("  autostart: " + (Get-TaskState))
  Write-Host ("  key: " + (Get-KeyState))
  if (Test-Path $LogFile) { Write-Host "  log ($LogFile, last 5):"; Get-Content $LogFile -Tail 5 | ForEach-Object { Write-Host "    $_" } }
  else { Write-Host '  log: none yet' }
}

function Find-PythonExe {
  foreach ($c in @('py -3', 'python', 'python3')) {
    $bin = ($c -split ' ')[0]; $pargs = ($c -split ' ')[1..9]
    try { & $bin @pargs --version 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { return $c } } catch {}
  }
  throw 'Python 3 not found. Install from https://www.python.org/downloads/ (tick "Add to PATH").'
}

# auto with no arg = on: ensure start-at-login regardless (idempotent).
function Enable-Auto {
  $pyCmd = Find-PythonExe
  $pyBin = ($pyCmd -split ' ')[0]; $pyArgs = ($pyCmd -split ' ')[1..9]
  $pyExe = (& $pyBin @pyArgs -c 'import sys; print(sys.executable)')
  New-Item -ItemType Directory -Force -Path $RouterDir | Out-Null
  # Pin the current port (like the Mac plist's ROUTER_PORT) so logon
  # starts honor custom ports instead of silently reverting to 8787.
  # The unified app (proxy child + tray) is the entrypoint, not router.py.
  @"
`$env:ROUTER_PORT = "$Port"
& "$pyExe" "$AppPy" >> "$LogFile" 2>&1
"@ | Set-Content $Wrapper -Encoding utf8
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$Wrapper`"" -WorkingDirectory $RouterDir
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  try { Enable-ScheduledTask -TaskName $TaskName | Out-Null } catch {}
  Write-Host "autostart: on (task $TaskName enabled, starts at logon)"
}

function Invoke-Start([string]$P) {
  if (Test-RouterUp $P) { Write-Host "router already up on $P"; return }
  Write-Host "starting router (port $P)..."
  try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { Write-Host "  task start: $_" }
  for ($i = 0; $i -lt 8; $i++) {
    if (Test-RouterUp $P) { Write-Host 'router is up'; return }
    Start-Sleep 1
  }
  # Fallback: run in this window so the user sees errors.
  $pyCmd = Find-PythonExe
  $pyBin = ($pyCmd -split ' ')[0]; $pyArgs = ($pyCmd -split ' ')[1..9]
  Write-Host "  task didn't answer; fallback: $pyBin $AppPy (Ctrl+C to stop)"
  $env:ROUTER_PORT = $P
  & $pyBin @pyArgs $AppPy --no-tray
}

function Invoke-Stop {
  Write-Host 'stopping router (keeps task for next login)...'
  try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
  $procs = @(Get-RouterProcess)
  foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force } catch {} }
  Start-Sleep 1
  if (Test-RouterUp $Port) { Write-Host "still answering on $Port (another copy running?)"; exit 1 }
  Write-Host 'stopped'
}

function Invoke-Auto([string]$A) {
  if (-not $A) { $A = 'on' }
  switch ($A) {
    'on' { Enable-Auto }
    'off' {
      try { Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null; Write-Host "autostart: off (task $TaskName disabled; router left as-is)" }
      catch { Write-Host "autostart: off (task $TaskName missing - nothing to disable)" }
    }
    'status' { Write-Host ("autostart: " + (Get-TaskState)) }
    default { Write-Host "unknown auto arg: $A (use on|off|status)"; exit 1 }
  }
}

function Invoke-RouterPy([string]$Name, [string[]]$Extra) {
  $pyCmd = Find-PythonExe
  $pyBin = ($pyCmd -split ' ')[0]; $pyArgs = ($pyCmd -split ' ')[1..9]
  $script = Join-Path $RouterDir $Name
  if (-not (Test-Path $script)) { Write-Host "$Name not installed in $RouterDir (re-run the installer)"; exit 1 }
  & $pyBin @pyArgs $script @Extra
}

function Invoke-Restart([string]$P) {
  Invoke-Stop
  Invoke-Start $P
}

function Invoke-Open([string]$P) {
  $url = "http://127.0.0.1:$P/ui"
  Write-Host "opening $url"
  Start-Process $url
}

function Invoke-Uninstall([string]$A) {
  Write-Host 'uninstalling mlaude...'
  Invoke-Stop
  try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null; Write-Host '  removed task ClaudeRouter' } catch {}
  try { Unregister-ScheduledTask -TaskName $SyncTaskName -Confirm:$false | Out-Null; Write-Host '  removed task ClaudeRouterSync' } catch {}
  if (Test-Path $RouterDir) { Remove-Item -Recurse -Force $RouterDir; Write-Host "  removed $RouterDir" }
  if ($A -eq '--keys') {
    foreach ($k in @('META_API_KEY', 'OPENCODE_API_KEY', 'ANTHROPIC_API_KEY')) {
      [Environment]::SetEnvironmentVariable($k, $null, 'User')
    }
    Write-Host '  removed stored keys'
  } else {
    Write-Host '  keys kept (re-run with --keys to remove them too)'
  }
  Write-Host '  manual cleanup: mlaude function in $PROFILE'
}

function Enable-SyncTask {
  $pyCmd = Find-PythonExe
  $pyBin = ($pyCmd -split ' ')[0]; $pyArgs = ($pyCmd -split ' ')[1..9]
  $pyExe = (& $pyBin @pyArgs -c 'import sys; print(sys.executable)')
  New-Item -ItemType Directory -Force -Path $RouterDir | Out-Null
  @"
& "$pyExe" "$SyncPy" --prune >> "$SyncLog" 2>&1
"@ | Set-Content $SyncWrapper -Encoding utf8
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$SyncWrapper`"" -WorkingDirectory $RouterDir
  $trigger = New-ScheduledTaskTrigger -Daily -At 3am
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $SyncTaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  Write-Host 'daily sync: on'
}

function Get-SyncState {
  try {
    $t = Get-ScheduledTask -TaskName $SyncTaskName -ErrorAction Stop
    if ($t.State -eq 'Disabled') { return 'off (Disabled)' }
    return "on ($($t.State), daily)"
  } catch { return 'off (no task)' }
}

function Invoke-AutoSync([string[]]$R) {
  $A = if ($R.Count -gt 0 -and $R[0]) { $R[0] } else { 'status' }
  switch ($A) {
    'on' { Enable-SyncTask }
    'off' {
      try { Unregister-ScheduledTask -TaskName $SyncTaskName -Confirm:$false | Out-Null; Write-Host 'daily sync: off' }
      catch { Write-Host 'daily sync: off (no task)' }
    }
    'status' { Write-Host ("daily sync: " + (Get-SyncState)) }
    default { Write-Host "unknown auto sync arg: $A (use on|off|status)"; exit 1 }
  }
}

switch ($Cmd.ToLower()) {
  'status' { Invoke-Status $Port }
  'start' { Invoke-Start $Port }
  'stop' { Invoke-Stop }
  'restart' { Invoke-Restart $Port }
  'open' { Invoke-Open $Port }
  'sync' { Invoke-RouterPy 'router\sync.py' @args }
  'keys' {
    # $Arg holds the first word, $args the rest: `keys set opencode`.
    $rest = @()
    if ($Arg) { $rest += $Arg }
    $rest += $args
    if ($rest.Count -eq 0) { $rest = @('list') }
    Invoke-RouterPy 'router\keys.py' $rest
  }
  'auto' {
    if ($Arg -eq 'sync') { Invoke-AutoSync $Rest } else { Invoke-Auto $Arg }
  }
  'uninstall' { Invoke-Uninstall $Arg }
  'help' { Show-Help }
  '-h' { Show-Help }
  '--help' { Show-Help }
  default { Write-Host "unknown command: $Cmd"; Show-Help; exit 1 }
}
