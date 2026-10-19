# mlaude verify — Windows. Mirrors verify.sh.
# Usage: .\verify.ps1 [-Port 8787]
param([string]$Port = $(if ($env:ROUTER_PORT) { $env:ROUTER_PORT } else { '8787' }))
$fail = 0
Write-Host "==> mlaude verify (port $Port)"

# NOTE: no -SkipHttpErrorCheck anywhere below: it doesn't exist on Windows
# PowerShell 5.1, which this script supports. Non-2xx answers throw there,
# so probes 1-2 read the status back out of the exception (works on 7+ too).
# Any HTTP answer (even Anthropic's root 404) counts as up.
try {
  $r = Invoke-WebRequest "http://127.0.0.1:$Port/" -TimeoutSec 5 -UseBasicParsing
  if (([int]$r.StatusCode -eq 404) -or ($r.Content -match 'Anthropic API')) { Write-Host "  PASS: GET / routes to Anthropic (HTTP $([int]$r.StatusCode))" }
  else { Write-Host '  FAIL: GET / wrong body'; $fail++ }
} catch {
  if ($_.Exception.Response) { Write-Host '  PASS: GET / answered with an HTTP error status (router is up)' }
  else { Write-Host "  FAIL: GET / unreachable: $_"; $fail++ }
}

# 2. Non-muse model forwards to Anthropic (401 without key proves the forward path).
try {
  $resp = Invoke-WebRequest "http://127.0.0.1:$Port/v1/messages" -Method Post -TimeoutSec 10 -UseBasicParsing `
    -ContentType 'application/json' -Body '{"model":"claude-sonnet-4-5","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
  Write-Host "  FAIL: claude-* POST returned $([int]$resp.StatusCode), expected 401"; $fail++
} catch {
  $errResp = $_.Exception.Response
  $code = if ($errResp) { [int]$errResp.StatusCode } else { 0 }
  if ($code -eq 401) { Write-Host '  PASS: claude-* POST forwards to api.anthropic.com (401 as expected)' }
  elseif ($code -gt 0) { Write-Host "  FAIL: claude-* POST returned $code, expected 401"; $fail++ }
  else { Write-Host "  FAIL: claude-* POST error: $_"; $fail++ }
}

# 3. muse-* model served through Meta, translated back.
try {
  $b = Invoke-WebRequest "http://127.0.0.1:$Port/v1/messages" -Method Post -TimeoutSec 25 -UseBasicParsing `
    -ContentType 'application/json' `
    -Headers @{ 'x-api-key' = 'test'; 'anthropic-version' = '2023-06-01' } `
    -Body '{"model":"muse-spark-1.3","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}'
  if ($b.Content -match '"type":"message"') { Write-Host '  PASS: muse-spark POST returns Anthropic-shaped message via Meta' }
  else { Write-Host "  FAIL: muse-spark POST bad body: $($b.Content.Substring(0, [Math]::Min(300, $b.Content.Length)))"; $fail++ }
} catch { Write-Host "  FAIL: muse-spark POST error: $_"; $fail++ }

# 4. Turns Meta can't map exactly are sanitized, not failed: stop_sequences
#    makes Meta's API 400, so the router drops it (logged) and serves the turn
#    via Responses instead.
try {
  $s = Invoke-WebRequest "http://127.0.0.1:$Port/v1/messages" -Method Post -TimeoutSec 25 -UseBasicParsing `
    -ContentType 'application/json' `
    -Headers @{ 'x-api-key' = 'test'; 'anthropic-version' = '2023-06-01' } `
    -Body '{"model":"muse-spark-1.3","max_tokens":5,"stop_sequences":["x"],"messages":[{"role":"user","content":"hi"}]}'
  if ($s.Content -match '"type":"message"') { Write-Host '  PASS: stop_sequences turn sanitized to Anthropic-shaped message (no 400)' }
  else { Write-Host "  FAIL: stop_sequences turn bad body: $($s.Content.Substring(0, [Math]::Min(300, $s.Content.Length)))"; $fail++ }
} catch { Write-Host "  FAIL: stop_sequences turn error: $_"; $fail++ }

Write-Host "==> done, $fail failed"
exit $fail
