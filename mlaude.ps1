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
$RouterDir = Join-Path $env:USERPROFILE '.local\share\claude-router'
$RouterPy = Join-Path $RouterDir 'router.py'
$Wrapper = Join-Path $RouterDir 'start-router.ps1'
$LogFile = Join-Path $RouterDir 'router.log'
$Port = if ($Port) { $Port } elseif ($env:ROUTER_PORT) { $env:ROUTER_PORT } else { '8787' }
# `mlaude status 8788` treats the extra arg as a port.
if (($Cmd -eq 'status' -or $Cmd -eq 'start') -and ($Arg -match '^\d+$')) { $Port = $Arg; $Arg = '' }

function Show-Help {
  @'
mlaude -- claude-router control (Windows)

  mlaude status [port]   router up? task state? key present? log tail
  mlaude start  [port]   start now (Scheduled Task, fallback: python window)
  mlaude stop            stop now (keeps task; use `mlaude auto off` to disable login start)
  mlaude auto [on|off|status]  no arg = on: ensure start-at-login regardless (idempotent)
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
  @" 
& "$pyExe" "$RouterPy" >> "$LogFile" 2>&1
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
  Write-Host "  task didn't answer; fallback: $pyBin $RouterPy (Ctrl+C to stop)"
  $env:ROUTER_PORT = $P
  & $pyBin @pyArgs $RouterPy
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

switch ($Cmd.ToLower()) {
  'status' { Invoke-Status $Port }
  'start' { Invoke-Start $Port }
  'stop' { Invoke-Stop }
  'auto' { Invoke-Auto $Arg }
  'help' { Show-Help }
  '-h' { Show-Help }
  '--help' { Show-Help }
  default { Write-Host "unknown command: $Cmd"; Show-Help; exit 1 }
}
