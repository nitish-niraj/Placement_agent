# PIA external health watcher — the eye outside the stack.
#
# The in-app watchdog (worker) covers DLQ/queue/heartbeat while a worker
# lives. This script covers the opposite case: Docker down, API wedged, or
# worker silently dead. Run it from Windows Task Scheduler every 10 minutes:
#   powershell -NoProfile -File E:\agent\placement-intelligence\infrastructure\health-watch.ps1
# Secrets are READ from infrastructure/.env (never stored here, never logged).
# Exit codes: 0 = healthy (quiet), 1 = alert sent, 2 = watcher itself broken.
param(
  [string]$BaseUrl = "http://localhost:8000",
  [int]$QueueWarnDepth = 500,
  [string]$ForceState = ""  # TEST ONLY: "down" simulates total outage
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $here "health-watch.log"

function Log($msg) {
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" | Out-File $log -Append -Encoding utf8
}

function Send-Alert($text) {
  $envMap = @{}
  Get-Content (Join-Path $here ".env") | Where-Object { $_ -match '^\w+=' -and $_ -notmatch '^#' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; $envMap[$k] = $v
  }
  $uri = "https://api.telegram.org/bot$($envMap['TELEGRAM_BOT_TOKEN'])/sendMessage"
  $body = @{ chat_id = $envMap['TELEGRAM_CHAT_ID']; text = $text; parse_mode = "HTML" } | ConvertTo-Json
  Invoke-RestMethod -Uri $uri -Method Post -Body $body -ContentType "application/json" -TimeoutSec 20 | Out-Null
}

try {
  if ($ForceState -eq "down") { throw "forced test outage" }
  $h = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 20
  $bad = @()
  if ($h.status -ne "ok") { $bad += "health status: $($h.status)" }
  foreach ($name in @("postgres", "redis", "minio", "evolution_api", "worker")) {
    $check = $h.checks.$($name)
    if ($check -ne "ok") { $bad += "${name}: $check" }
  }
  $depth = [int]($h.meta.queue_depth)
  if ($depth -ge $QueueWarnDepth) { $bad += "queue depth $depth (flood?)" }
  if ($bad.Count -eq 0) { Log "ok depth=$depth"; exit 0 }
  # NOTE: ASCII-only message text — Windows PowerShell reads BOM-less scripts
  # as ANSI, so any emoji here arrives mangled and Telegram 400s the send.
  $msg = "[PIA watcher] " + ($bad -join " / ")
  Send-Alert $msg; Log "ALERT: $($bad -join '; ')"; exit 1
} catch {
  # NOTE: ASCII-only literals (see above); runtime exception text is safe.
  $detail = [string]$_.Exception.Message
  try {
    Send-Alert ("[PIA watcher] API unreachable: " + $detail)
    Log "ALERT: api unreachable"
  } catch {
    Log "WATCHER-BROKEN: $($_.Exception.Message)"
    exit 2
  }
  exit 1
}
