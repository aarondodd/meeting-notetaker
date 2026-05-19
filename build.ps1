# Windows production build. PowerShell 5+.
# Usage:  .\build.ps1
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$Venv = ".venv"
$Python = "python"

if (-not (Test-Path $Venv)) {
    & $Python -m venv $Venv
}

& "$Venv\Scripts\Activate.ps1"

python -m pip install --upgrade pip
pip install -r requirements-dev.txt

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

pyinstaller --noconfirm --clean meeting_notetaker.spec

Write-Host ""
Write-Host "Built: $(Get-Location)\dist\meeting-notetaker.exe"
