# KARMA Super Console local start helper (Windows / PowerShell)
# Self-check -> ensure directories -> foreground uvicorn, tee logs to logs\.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# Prefer a local venv if present; otherwise use PATH python.
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) { $py = $venvPy } else { $py = "python" }

# 1) Self-check: abort on any FAIL instead of starting in a broken state.
Write-Host "== Self-check ==" -ForegroundColor Cyan
& $py scripts/selfcheck.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "Self-check failed (see above); startup aborted." -ForegroundColor Red
    exit 1
}

# 2) Directory convention: create logs / backups / data if missing.
foreach ($d in @("logs", "backups", "data")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null
}
$logDir = Join-Path $Root "logs"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$outLog = Join-Path $logDir ("server_{0}.log" -f $stamp)

# 3) Default: background scheduler off for local manual mode; keep if already set.
if (-not $env:ENABLE_BACKGROUND_SCHEDULER) { $env:ENABLE_BACKGROUND_SCHEDULER = "0" }

Write-Host ("== KARMA Super Console: http://{0}:{1} ==" -f $BindHost, $Port) -ForegroundColor Green
Write-Host ("Log file: {0}" -f $outLog) -ForegroundColor DarkGray
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray

# 4) Foreground run; merge stdout/stderr and tee into a per-start log file.
& $py -m uvicorn app.main:app --host $BindHost --port $Port 2>&1 |
    Tee-Object -FilePath $outLog
