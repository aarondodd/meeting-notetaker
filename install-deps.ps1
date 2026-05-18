# Install all Python dependencies into the local venv.
# Usage:  .\install-deps.ps1
#
# Two-step install because Resemblyzer (speaker embedding) pulls original
# webrtcvad as a hard dep, and that package has no Windows wheel for
# Python 3.10+. Installing Resemblyzer with --no-deps skips that
# resolution; the deps it actually uses at runtime (librosa, scipy, torch,
# numpy, webrtcvad-wheels) are already pinned in requirements.txt.
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
pip install --no-deps "Resemblyzer>=0.1.4"

Write-Host ""
Write-Host "Done. Run:  python main.py"
