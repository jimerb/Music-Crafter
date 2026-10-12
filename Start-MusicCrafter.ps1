param([int]$Port = 8768, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$dataDir = Join-Path $projectDir 'app/data'
$url = "http://127.0.0.1:$Port/"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
try {
    $ready = Invoke-RestMethod -Uri "${url}api/health" -TimeoutSec 2
    if ($ready.app -eq 'Music Crafter') {
        if (-not $NoBrowser) { Start-Process $url }
        Write-Output "Music Crafter is running at $url"
        exit 0
    }
} catch { }
$candidates = @(
    $env:MUSIC_CRAFTER_PYTHON,
    (Join-Path $projectDir '.venv/Scripts/python.exe'),
    (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')
)
$installedPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($installedPython -and $installedPython.Source -notmatch 'WindowsApps') { $candidates += $installedPython.Source }
$pythonBin = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        & $candidate -c 'import sys, numpy; assert sys.version_info >= (3, 10)' 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonBin = $candidate; break }
    }
}
if (-not $pythonBin) { throw 'Music Crafter needs Python 3.10+ with NumPy. Follow README setup or set MUSIC_CRAFTER_PYTHON to an existing installation.' }
$entry = Join-Path $projectDir 'app/run.py'
$process = Start-Process -FilePath $pythonBin -ArgumentList @('-u', ('"' + $entry + '"'), '--port', $Port) -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir 'server.log') -RedirectStandardError (Join-Path $dataDir 'server-errors.log') -PassThru
Set-Content -LiteralPath (Join-Path $dataDir 'server.pid') -Value $process.Id -Encoding ascii
$healthy = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ($process.HasExited) { throw "Music Crafter did not start. See app/data/server-errors.log." }
    try {
        $ready = Invoke-RestMethod -Uri "${url}api/health" -TimeoutSec 1
        if ($ready.app -eq 'Music Crafter') { $healthy = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 250
}
if (-not $healthy) { throw 'Music Crafter is starting slowly. See app/data/server-errors.log.' }
if (-not $NoBrowser) { Start-Process $url }
Write-Output "Music Crafter is running at $url"
