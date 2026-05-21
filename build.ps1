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
#
# CRITICAL: this script activated the dev venv earlier, which sets
# VIRTUAL_ENV and prepends .venv\Scripts to PATH. The frozen .exe, when
# launched in that environment, picks up the venv's site-packages as an
# import location, so dependencies that are missing from the bundle but
# present in the dev venv would appear OK and slip through the gate. We
# launch the gate in a sanitized child process: clear VIRTUAL_ENV,
# PYTHONHOME, PYTHONPATH, and reset PATH to the system default so the
# .exe behaves exactly like it would on a user's machine. (See
# 2026-05-22 incident: sounddevice was missing from the bundle but the
# gate said OK because the activated venv was leaking through.)
Write-Host ""
Write-Host "==> Running post-build dependency self-test (in clean env)..."
$ExePath = "$(Get-Location)\dist\meeting-notetaker.exe"

$SysPath = [Environment]::GetEnvironmentVariable("PATH", "Machine")
if (-not $SysPath) { $SysPath = "$env:SystemRoot;$env:SystemRoot\System32" }

# Use Start-Process so we can hand the child a clean environment block.
# PassThru gives us the Process object; -Wait blocks. -NoNewWindow keeps
# output in this console. We redirect stdout/stderr to temp files and
# stream them back so the report still surfaces in the build log.
$StdOutFile = [System.IO.Path]::GetTempFileName()
$StdErrFile = [System.IO.Path]::GetTempFileName()
try {
    $Proc = Start-Process -FilePath $ExePath `
        -ArgumentList "--check-deps" `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $StdOutFile `
        -RedirectStandardError $StdErrFile `
        -Environment @{
            "PATH" = $SysPath
            "VIRTUAL_ENV" = ""
            "PYTHONHOME" = ""
            "PYTHONPATH" = ""
        }
    Get-Content $StdOutFile | Write-Host
    $errText = Get-Content $StdErrFile -Raw
    if ($errText) { Write-Host $errText }
    $DepCheckExit = $Proc.ExitCode
} catch {
    # Older PowerShell versions (< 6) don't support -Environment on
    # Start-Process. Fall back to a Cmd one-liner that clears the
    # variables before invoking the .exe.
    Write-Host "(Start-Process -Environment unsupported; using cmd fallback)"
    $CmdLine = "set VIRTUAL_ENV=&& set PYTHONHOME=&& set PYTHONPATH=&& " + `
               "set PATH=$SysPath&& `"$ExePath`" --check-deps"
    cmd.exe /c $CmdLine
    $DepCheckExit = $LASTEXITCODE
} finally {
    Remove-Item $StdOutFile -ErrorAction SilentlyContinue
    Remove-Item $StdErrFile -ErrorAction SilentlyContinue
}

if ($DepCheckExit -ne 0) {
    Write-Host ""
    Write-Error "Build produced a binary with MISSING dependencies (exit $DepCheckExit). See report above; add the missing module(s) to meeting_notetaker.spec hiddenimports or collect_all() and rebuild."
    exit 1
}

Write-Host ""
Write-Host "Built: $ExePath"
