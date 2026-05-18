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
# Resemblyzer pulls original webrtcvad as a hard dep; no Windows wheel
# for Python 3.10+, so we install with --no-deps. The transitive deps
# it actually uses (librosa, scipy, torch, numpy, webrtcvad-wheels)
# are already in requirements.txt. See requirements.txt for the long
# explanation.
pip install --no-deps Resemblyzer>=0.1.4

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

pyinstaller --noconfirm --clean meeting_notetaker.spec

Write-Host ""
Write-Host "Built: $(Get-Location)\dist\meeting-notetaker.exe"
