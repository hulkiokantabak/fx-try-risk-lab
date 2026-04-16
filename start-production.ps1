$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $projectRoot ".env.production"

if (-not (Test-Path $python)) {
    throw "Local Python environment not found at .venv\\Scripts\\python.exe"
}

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $parts = $line -split "=", 2
        if ($parts.Length -ne 2) {
            return
        }
        [System.Environment]::SetEnvironmentVariable($parts[0], $parts[1])
    }
}

if (-not $env:FX_ENVIRONMENT) { $env:FX_ENVIRONMENT = "production" }
if (-not $env:FX_HOST) { $env:FX_HOST = "0.0.0.0" }
if (-not $env:FX_PORT) { $env:FX_PORT = "8000" }
if (-not $env:FX_RELOAD) { $env:FX_RELOAD = "false" }
if (-not $env:FX_FORWARDED_ALLOW_IPS) { $env:FX_FORWARDED_ALLOW_IPS = "127.0.0.1" }

if (-not $env:FX_SESSION_SECRET) {
    throw "FX_SESSION_SECRET must be set before running production mode."
}

Write-Host "Starting FX TRY Risk Lab in production mode on http://$($env:FX_HOST):$($env:FX_PORT)" -ForegroundColor Green
& $python -m app.serve
