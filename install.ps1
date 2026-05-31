# Meeting Notetaker -- Windows install bootstrap.
#
# Fresh-machine path: download the latest release's Inno Setup
# installer (.exe) and launch it interactively. The user gets the
# full installer wizard; this script just removes the manual steps
# of finding the Releases page, picking the right asset, and copying
# the URL.
#
# Usage (from any Windows PowerShell 5.1+ or PowerShell 7+):
#
#   iwr -useb https://raw.githubusercontent.com/aarondodd/meeting-notetaker/main/install.ps1 | iex
#
# Or, if cloned locally:
#
#   .\install.ps1
#
# For an existing install, use upgrade.ps1 instead -- it surfaces
# the currently-installed version so you can decide whether the
# upgrade is worth running.

[CmdletBinding()]
param(
    # Override the repo if you fork. Defaults match the canonical
    # public repo coordinates encoded in meeting_notetaker/utils/updater.py.
    [string]$Owner = "aarondodd",
    [string]$Repo  = "meeting-notetaker",
    # Skip the Y/N prompt -- useful for automated provisioning.
    # Treats -Yes the same as typing Y at the prompt.
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

# Older PowerShell hosts (5.1 on Windows 10) default to TLS 1.0 which
# api.github.com no longer accepts. Force TLS 1.2 explicitly.
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.SecurityProtocolType]::Tls12 -bor `
        [Net.ServicePointManager]::SecurityProtocol
} catch {
    # Older .NET runtimes may not enumerate Tls12; ignore and hope
    # the system default is sane. Will fail at Invoke-RestMethod if
    # not, which surfaces a clear network error.
}

function Get-LatestRelease {
    param(
        [string]$Owner,
        [string]$Repo
    )
    $url = "https://api.github.com/repos/$Owner/$Repo/releases/latest"
    Write-Host "Checking latest release on GitHub..." -ForegroundColor Cyan
    try {
        return Invoke-RestMethod -Uri $url -UseBasicParsing -Headers @{
            "User-Agent" = "meeting-notetaker-install-script"
        }
    } catch {
        throw "Could not reach the GitHub releases API ($url). " +
              "Check your network connection. Underlying error: $_"
    }
}

function Find-InstallerAsset {
    param([object]$Release)
    # The release pipeline (.github/workflows/release.yml) uploads a
    # single artifact named meeting-notetaker-setup-<version>.exe.
    # Match defensively in case future releases rename or add assets.
    $candidate = $Release.assets | Where-Object {
        $_.name -like "meeting-notetaker-setup-*.exe"
    } | Select-Object -First 1
    if (-not $candidate) {
        throw "Latest release $($Release.tag_name) has no " +
              "meeting-notetaker-setup-*.exe asset attached. The " +
              "release pipeline may still be running -- try again " +
              "in a few minutes."
    }
    return $candidate
}

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return "{0:N1} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N1} MB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N1} KB" -f ($Bytes / 1KB) }
    return "$Bytes bytes"
}

function Download-Installer {
    param(
        [string]$Url,
        [string]$Destination
    )
    Write-Host ""
    Write-Host "Downloading installer to $Destination ..." -ForegroundColor Cyan
    # ProgressPreference=Continue keeps the built-in PowerShell
    # progress bar visible during the download. Older PS hosts (5.1)
    # render it noticeably slower than the actual transfer when set
    # to Continue, so prefer 'SilentlyContinue' for a faster download
    # at the cost of no on-screen progress bar.
    $prior = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    } finally {
        $ProgressPreference = $prior
    }
    if (-not (Test-Path -LiteralPath $Destination)) {
        throw "Download finished but $Destination is missing. " +
              "Disk full? Antivirus interception?"
    }
    $size = (Get-Item -LiteralPath $Destination).Length
    Write-Host "Downloaded $(Format-Size $size)." -ForegroundColor Green
}

function Confirm-Prompt {
    param(
        [string]$Question,
        [switch]$Yes
    )
    if ($Yes) {
        Write-Host "$Question [Y/n] (auto-yes via -Yes flag)" -ForegroundColor Yellow
        return $true
    }
    while ($true) {
        $reply = Read-Host "$Question [Y/n]"
        if ([string]::IsNullOrWhiteSpace($reply)) { return $true }
        switch -Regex ($reply.Trim()) {
            '^[yY]([eE][sS])?$' { return $true }
            '^[nN]([oO])?$'     { return $false }
            default { Write-Host "Please answer Y or N." -ForegroundColor Yellow }
        }
    }
}

# ---- main -----------------------------------------------------------

Write-Host ""
Write-Host "Meeting Notetaker -- Windows install" -ForegroundColor Cyan
Write-Host "===================================="

$release = Get-LatestRelease -Owner $Owner -Repo $Repo
$asset = Find-InstallerAsset -Release $release

Write-Host ""
Write-Host ("Latest release: {0}" -f $release.tag_name) -ForegroundColor Green
Write-Host ("  Asset:        {0}" -f $asset.name)
Write-Host ("  Size:         {0}" -f (Format-Size $asset.size))
Write-Host ("  Published:    {0}" -f $release.published_at)
Write-Host ("  URL:          {0}" -f $release.html_url)
Write-Host ""

if (-not (Confirm-Prompt -Question "Download and install $($release.tag_name) now?" -Yes:$Yes)) {
    Write-Host "Cancelled by user." -ForegroundColor Yellow
    exit 0
}

# Temp path -- caller can keep the file if the install gets canceled
# half-way through, but most users want it gone. The installer doesn't
# need it after Setup completes; Inno Setup unpacks into AppData /
# Program Files immediately.
$temp = Join-Path -Path $env:TEMP -ChildPath $asset.name
try {
    Download-Installer -Url $asset.browser_download_url -Destination $temp

    Write-Host ""
    Write-Host "Launching installer..." -ForegroundColor Cyan
    # -Wait so the script doesn't exit until the user finishes the
    # wizard. -PassThru gives us the exit code for a clean message.
    $proc = Start-Process -FilePath $temp -Wait -PassThru
    if ($proc.ExitCode -eq 0) {
        Write-Host ""
        Write-Host "Install complete." -ForegroundColor Green
        Write-Host ("Launch Meeting Notetaker from the Start Menu or " +
                   "from the desktop shortcut if you opted in.")
    } else {
        Write-Host ""
        Write-Host ("Installer exited with code $($proc.ExitCode). " +
                    "If the wizard was cancelled, this is expected; " +
                    "otherwise check the installer's log under " +
                    "%TEMP%\Setup Log*.txt.") -ForegroundColor Yellow
    }
} finally {
    # Clean up the downloaded installer so a half-finished install
    # doesn't leave a 250+ MB file in %TEMP%.
    if (Test-Path -LiteralPath $temp) {
        try {
            Remove-Item -LiteralPath $temp -Force -ErrorAction Stop
        } catch {
            Write-Host ("Could not remove $temp -- delete it manually " +
                        "when the installer is closed.") -ForegroundColor Yellow
        }
    }
}
