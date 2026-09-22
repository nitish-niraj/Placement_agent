# Transcript/evidence retention: keep newest 30, delete older (default dry-run).
# Usage: .\scripts\clean-transcripts.ps1 [-Apply] [-Keep 30]
param([switch]$Apply, [int]$Keep = 30)
$dir = Join-Path (Split-Path -Parent $PSScriptRoot) "transcripts"
$files = Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending
Write-Host "$($files.Count) files, keeping newest $Keep"
$old = @($files | Select-Object -Skip $Keep)
foreach ($f in $old) {
  if ($Apply) { Remove-Item -LiteralPath $f.FullName -Force; Write-Host "deleted $($f.Name)" }
  else { Write-Host "would delete $($f.Name)" }
}
if (-not $Apply) { Write-Host "dry-run: re-run with -Apply to delete" }
