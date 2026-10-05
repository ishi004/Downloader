$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
}
& '.venv/Scripts/python.exe' -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed.' }
& '.venv/Scripts/python.exe' scripts/setup_tools.py
if ($LASTEXITCODE -ne 0) { throw 'FFmpeg setup failed.' }
& '.venv/Scripts/python.exe' scripts/check_tools.py
if ($LASTEXITCODE -ne 0) { throw 'Tool capability check failed.' }

