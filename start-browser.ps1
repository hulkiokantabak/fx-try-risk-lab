$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python scripts\build_browser_data.py
python scripts\validate_browser_bundle.py
python -m http.server 8080 --directory docs
