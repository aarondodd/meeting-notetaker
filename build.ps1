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

# Post-build gate: invoke the freshly-built .exe with --check-deps. If
# any dependency is MISSING (PyInstaller's static analysis missed a hidden
# import, or a contrib hook silently failed), exit non-zero so the build
# fails loudly here rather than producing a binary that will skip features
# at runtime. SKIP rows (platform-not-applicable) are not failures.
Write-Host ""
Write-Host "==> Running post-build dependency self-test..."
$ExePath = "$(Get-Location)\dist\meeting-notetaker.exe"
& $ExePath --check-deps
$DepCheckExit = $LASTEXITCODE
if ($DepCheckExit -ne 0) {
    Write-Host ""
    Write-Error "Build produced a binary with MISSING dependencies (exit $DepCheckExit). See report above; add the missing module(s) to meeting_notetaker.spec hiddenimports or collect_all() and rebuild."
    exit 1
}

Write-Host ""
Write-Host "Built: $ExePath"
