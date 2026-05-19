# Install all Python dependencies into the local venv.
# Usage:  .\install-deps.ps1
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$Venv = ".venv"
$Python = "python"

if (-not (Test-Path $Venv)) {
    Write-Host "Creating virtualenv at $Venv..."
    & $Python -m venv $Venv
}

& "$Venv\Scripts\Activate.ps1"

python -m pip install --upgrade pip
pip install -r requirements-dev.txt

Write-Host ""
Write-Host "Done. Run:  python main.py"
