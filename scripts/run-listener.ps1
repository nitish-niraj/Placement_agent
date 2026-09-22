# PIA Teams listener runner (2026-09-22 hygiene).
# Usage: .\scripts\run-listener.ps1 -MeetingUrl "<link>" [-Minutes 4]
param(
  [Parameter(Mandatory = $true)][string]$MeetingUrl,
  [int]$Minutes = 4
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Get-Content "$root\infrastructure\.env" |
  Where-Object { $_ -match '^\w+=' -and $_ -notmatch '^#' } |
  ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
& "$root\.venv\Scripts\python" -m pia_worker.teams.listener $MeetingUrl --minutes $Minutes
