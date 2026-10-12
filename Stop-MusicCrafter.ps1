$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$pidFile = Join-Path $projectDir 'app/data/server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'Music Crafter is already stopped.'; exit 0 }
$serverProcessId = [int](Get-Content -LiteralPath $pidFile -Raw)
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $serverProcessId" -ErrorAction SilentlyContinue
$expectedEntry = Join-Path $projectDir 'app/run.py'
if ($serverProcess -and $serverProcess.CommandLine -and $serverProcess.CommandLine.Contains($expectedEntry)) {
    Stop-Process -Id $serverProcessId
    Write-Output 'Music Crafter stopped.'
} elseif ($serverProcess) {
    throw 'The saved process is not the Music Crafter server. It was left running.'
} else { Write-Output 'Music Crafter is already stopped.' }
Remove-Item -LiteralPath $pidFile
