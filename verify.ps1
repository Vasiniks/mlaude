# mlaude verify — Windows. Mirrors verify.sh.
# Usage: .\verify.ps1 [-Port 8787]
param([string]$Port = $(if ($env:ROUTER_PORT) { $env:ROUTER_PORT } else { '8787' }))
$fail = 0
Write-Host "==> mlaude verify (port $Port)"

# 1. Router answers at all (Anthropic's root 404s, banner or empty, so HTTP 404
#    or the banner is the alive signal, not any particular body).
try {
  $r = Invoke-WebRequest "http://127.0.0.1:$Port/" -TimeoutSec 5 -SkipHttpErrorCheck
  if (($r.StatusCode -eq 404) -or ($r.Content -match 'Anthropic API')) { Write-Host "  PASS: GET / routes to Anthropic (HTTP $($r.StatusCode))" }
  else { Write-Host '  FAIL: GET / wrong body'; $fail++ }
} catch { Write-Host "  FAIL: GET / unreachable: $_"; $fail++ }

# 2. Non-muse model forwards to Anthropic (401 without key proves the forward path).
try {
  $resp = Invoke-WebRequest "http://127.0.0.1:$Port/v1/messages" -Method Post -TimeoutSec 10 `
    -ContentType 'application/json' -Body '{"model":"claude-sonnet-4-5","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' `
    -SkipHttpErrorCheck
  if ($resp.StatusCode -eq 401) { Write-Host '  PASS: claude-* POST forwards to api.anthropic.com (401 as expected)' }
  else { Write-Host "  FAIL: claude-* POST returned $($resp.StatusCode), expected 401"; $fail++ }
} catch { Write-Host "  FAIL: claude-* POST error: $_"; $fail++ }

# 3. muse-* model served through Meta, translated back.
try {
  $b = Invoke-WebRequest "http://127.0.0.1:$Port/v1/messages" -Method Post -TimeoutSec 25 `
    -ContentType 'application/json' `
    -Headers @{ 'x-api-key' = 'test'; 'anthropic-version' = '2023-06-01' } `
    -Body '{"model":"muse-spark-1.3","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}'
  if ($b.Content -match '"type":"message"') { Write-Host '  PASS: muse-spark POST returns Anthropic-shaped message via Meta' }
  else { Write-Host "  FAIL: muse-spark POST bad body: $($b.Content.Substring(0, [Math]::Min(300, $b.Content.Length)))"; $fail++ }
} catch { Write-Host "  FAIL: muse-spark POST error: $_"; $fail++ }

Write-Host "==> done, $fail failed"
exit $fail
