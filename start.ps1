$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Local Python environment not found at .venv\\Scripts\\python.exe"
}

if (-not $env:FX_HOST) { $env:FX_HOST = "127.0.0.1" }
if (-not $env:FX_PORT) { $env:FX_PORT = "8000" }
if (-not $env:FX_RELOAD) { $env:FX_RELOAD = "true" }
if (-not $env:FX_FORWARDED_ALLOW_IPS) { $env:FX_FORWARDED_ALLOW_IPS = "127.0.0.1" }

Write-Host "Starting FX TRY Risk Lab at http://$($env:FX_HOST):$($env:FX_PORT)" -ForegroundColor Green
& $python -m app.serve
