# Meeting Notetaker -- Windows upgrade.
#
# Detects the currently-installed version from the Inno Setup
# uninstall registry entries (both per-user HKCU and system-wide
# HKLM, so per-user installs upgrade cleanly), compares against the
# latest GitHub release, and prompts before downloading + launching
# the installer. The Inno Setup config uses CloseApplications +
# RestartApplications, so the installer closes the running app via
# Windows Restart Manager and relaunches it after.
#
# Usage:
#
#   iwr -useb https://raw.githubusercontent.com/aarondodd/meeting-notetaker/main/upgrade.ps1 | iex
#
# Or locally:
#
#   .\upgrade.ps1
#
# For a fresh install, use install.ps1 instead.

[CmdletBinding()]
param(
    [string]$Owner = "aarondodd",
    [string]$Repo  = "meeting-notetaker",
    # Force the upgrade even when the installed version is already
    # at or ahead of the latest release. Useful for reinstall /
    # repair flows or for testing a freshly-cut tag.
    [switch]$Force,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.SecurityProtocolType]::Tls12 -bor `
        [Net.ServicePointManager]::SecurityProtocol
} catch {
    # See install.ps1 for context.
}

# Inno Setup's AppId from installer.iss (line 18). Append "_is1" --
# Inno Setup writes its uninstall key under <AppId>_is1, NOT the bare
# AppId. The installer.iss comment explicitly notes the AppId is
# stable across releases, so this lookup is safe long-term.
$InnoAppId = "{B1F03D8E-7C29-4A6E-9B0F-9A6B7C0E1D2F}_is1"

function Get-InstalledVersion {
    # Inno Setup writes to HKCU when the install is per-user and to
    # HKLM when it elevated for a system-wide install. Check both;
    # whichever wins gives us the active install. If both exist
    # (rare, but possible after a per-user install followed by an
    # elevated reinstall), HKLM takes precedence because that's what
    # the Start Menu shortcut points to under that scenario.
    $candidates = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$InnoAppId",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$InnoAppId",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$InnoAppId"
    )
    foreach ($path in $candidates) {
        try {
            $key = Get-ItemProperty -Path $path -ErrorAction Stop
            if ($key -and $key.DisplayVersion) {
                return [pscustomobject]@{
                    DisplayVersion = $key.DisplayVersion
                    InstallLocation = $key.InstallLocation
                    Scope = if ($path -like "HKCU:*") { "per-user" } else { "system-wide" }
                    RegistryPath = $path
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Parse-SemVer {
    # Lenient semver parse: leading "v" allowed, optional pre-release
    # suffix discarded. Returns [version] for safe -gt / -lt math.
    # Throws on garbage so callers can decide whether to bail or warn.
    param([string]$Raw)
    if (-not $Raw) { throw "Empty version string." }
    $trim = $Raw.Trim()
    if ($trim.StartsWith("v") -or $trim.StartsWith("V")) {
        $trim = $trim.Substring(1)
    }
    # Strip pre-release / build suffix (anything after the first
    # non-version character) so 0.7.5-dev compares as 0.7.5. PowerShell
    # [version] is strict about extra components, but a 4-part
    # X.Y.Z.W is fine.
    if ($trim -match '^(\d+(\.\d+){0,3})') {
        return [version]$matches[1]
    }
    throw "Cannot parse version: $Raw"
}

function Get-LatestRelease {
    param([string]$Owner, [string]$Repo)
    $url = "https://api.github.com/repos/$Owner/$Repo/releases/latest"
    Write-Host "Checking latest release on GitHub..." -ForegroundColor Cyan
    try {
        return Invoke-RestMethod -Uri $url -UseBasicParsing -Headers @{
            "User-Agent" = "meeting-notetaker-upgrade-script"
        }
    } catch {
        throw "Could not reach the GitHub releases API ($url). " +
              "Check your network connection. Underlying error: $_"
    }
}

function Find-InstallerAsset {
    param([object]$Release)
    $candidate = $Release.assets | Where-Object {
        $_.name -like "meeting-notetaker-setup-*.exe"
    } | Select-Object -First 1
    if (-not $candidate) {
        throw "Latest release $($Release.tag_name) has no " +
              "meeting-notetaker-setup-*.exe asset attached."
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
    param([string]$Url, [string]$Destination)
    Write-Host ""
    Write-Host "Downloading installer to $Destination ..." -ForegroundColor Cyan
    $prior = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    } finally {
        $ProgressPreference = $prior
    }
    if (-not (Test-Path -LiteralPath $Destination)) {
        throw "Download finished but $Destination is missing."
    }
    $size = (Get-Item -LiteralPath $Destination).Length
    Write-Host "Downloaded $(Format-Size $size)." -ForegroundColor Green
}

function Confirm-Prompt {
    param([string]$Question, [switch]$Yes)
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
Write-Host "Meeting Notetaker -- Windows upgrade" -ForegroundColor Cyan
Write-Host "===================================="

$installed = Get-InstalledVersion
if (-not $installed) {
    Write-Host ""
    $msg = "No installed Meeting Notetaker detected (no Inno Setup " +
           "registry entry under $InnoAppId)."
    Write-Host $msg -ForegroundColor Yellow
    Write-Host "Use install.ps1 for a fresh install:"
    Write-Host "  iwr -useb https://raw.githubusercontent.com/$Owner/$Repo/main/install.ps1 | iex"
    exit 1
}

Write-Host ""
Write-Host ("Installed: {0} ({1})" -f $installed.DisplayVersion, $installed.Scope) -ForegroundColor Green
if ($installed.InstallLocation) {
    Write-Host ("  Location: {0}" -f $installed.InstallLocation)
}

$release = Get-LatestRelease -Owner $Owner -Repo $Repo
$asset = Find-InstallerAsset -Release $release

# Version comparison. Failures here are non-fatal -- we'd rather
# still offer the upgrade than block the user on a parse glitch
# from a custom build of the app.
$cmpResult = $null
try {
    $installedVer = Parse-SemVer -Raw $installed.DisplayVersion
    $latestVer    = Parse-SemVer -Raw $release.tag_name
    $cmpResult = $installedVer.CompareTo($latestVer)
} catch {
    Write-Host ("Version compare skipped: {0}" -f $_) -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("Latest release: {0}" -f $release.tag_name) -ForegroundColor Green
Write-Host ("  Asset:        {0}" -f $asset.name)
Write-Host ("  Size:         {0}" -f (Format-Size $asset.size))
Write-Host ("  Published:    {0}" -f $release.published_at)
Write-Host ("  URL:          {0}" -f $release.html_url)
Write-Host ""

if ($cmpResult -eq 0) {
    Write-Host ("You are already on the latest release " +
                "($($release.tag_name)).") -ForegroundColor Green
    if (-not $Force) {
        if (-not (Confirm-Prompt -Question "Reinstall anyway?" -Yes:$Yes)) {
            Write-Host "Nothing to do." -ForegroundColor Yellow
            exit 0
        }
    }
} elseif ($cmpResult -gt 0) {
    Write-Host ("Installed version $($installed.DisplayVersion) is " +
                "AHEAD of the latest release ($($release.tag_name)). " +
                "Looks like a local / dev build.") -ForegroundColor Yellow
    if (-not $Force) {
        if (-not (Confirm-Prompt -Question "Downgrade to the latest release?" -Yes:$Yes)) {
            Write-Host "Cancelled." -ForegroundColor Yellow
            exit 0
        }
    }
} else {
    if (-not (Confirm-Prompt -Question "Upgrade from $($installed.DisplayVersion) to $($release.tag_name)?" -Yes:$Yes)) {
        Write-Host "Cancelled by user." -ForegroundColor Yellow
        exit 0
    }
}

$temp = Join-Path -Path $env:TEMP -ChildPath $asset.name
try {
    Download-Installer -Url $asset.browser_download_url -Destination $temp

    Write-Host ""
    Write-Host "Launching installer..." -ForegroundColor Cyan
    Write-Host ("If Meeting Notetaker is currently running, Windows " +
                "Restart Manager will close it via the installer's " +
                "CloseApplications hook and relaunch it after the " +
                "upgrade.")
    # Interactive wizard; the user sees Inno Setup's upgrade prompts.
    # The installer's stable AppId means the existing install gets
    # replaced in place rather than stacking a second Add/Remove
    # Programs entry.
    $proc = Start-Process -FilePath $temp -Wait -PassThru
    if ($proc.ExitCode -eq 0) {
        Write-Host ""
        Write-Host "Upgrade complete." -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host ("Installer exited with code $($proc.ExitCode). " +
                    "If the wizard was cancelled, this is expected; " +
                    "otherwise check the installer's log under " +
                    "%TEMP%\Setup Log*.txt.") -ForegroundColor Yellow
    }
} finally {
    if (Test-Path -LiteralPath $temp) {
        try {
            Remove-Item -LiteralPath $temp -Force -ErrorAction Stop
        } catch {
            Write-Host ("Could not remove $temp -- delete it manually " +
                        "when the installer is closed.") -ForegroundColor Yellow
        }
    }
}
