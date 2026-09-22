# PIA Teams session refresh (2026-09-22 hygiene).
# Manual (MFA-safe): .\scripts\refresh-session.ps1 -MeetingUrl "<link>"
# Auto (programmatic, MFA still needs a phone tap):
#   .\scripts\refresh-session.ps1 -Auto -MeetingUrl "<link>"
param(
  [string]$MeetingUrl = "",
  [switch]$Auto
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Get-Content "$root\infrastructure\.env" |
  Where-Object { $_ -match '^\w+=' -and $_ -notmatch '^#' } |
  ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
if ($Auto) {
  if (-not $MeetingUrl) { throw "MeetingUrl required with -Auto" }
  & "$root\.venv\Scripts\python" -m pia_worker.teams.login_save --auto --meeting $MeetingUrl
} elseif ($MeetingUrl) {
  & "$root\.venv\Scripts\python" -m pia_worker.teams.login_save $MeetingUrl
} else {
  & "$root\.venv\Scripts\python" -m pia_worker.teams.login_save
}
