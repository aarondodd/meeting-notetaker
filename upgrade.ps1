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

# Inno Setup's AppId from installer.iss (line 18). Inno Setup
# appends "_is1" to the AppId for the uninstall registry key. The
# installer.iss comment notes the AppId is stable across releases,
# so this fast-path lookup is safe long-term. If the canonical
# key isn't found (custom build, registry quirks, etc.) the
# function falls back to enumerating uninstall keys and matching
# by DisplayName.
$InnoAppId = "{B1F03D8E-7C29-4A6E-9B0F-9A6B7C0E1D2F}_is1"
$DisplayNameMatch = "Meeting Notetaker"

# Three roots cover every scope an Inno Setup install can land in:
#   HKLM\...\Uninstall              -- system-wide 64-bit
#   HKLM\...\WOW6432Node\...\Uninstall -- system-wide 32-bit (rare for us)
#   HKCU\...\Uninstall              -- per-user (default, since
#                                       installer.iss sets
#                                       PrivilegesRequired=lowest)
$UninstallRoots = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
)

function Read-UninstallKey {
    # Read one uninstall key and return a pscustomobject if it looks
    # like ours. -LiteralPath so the curly braces in the GUID-shaped
    # subkey name don't get interpreted as wildcards by the registry
    # provider (which is the bug that broke the v0.7.5 first cut).
    param([string]$Root, [string]$Subkey)
    $path = Join-Path -Path $Root -ChildPath $Subkey
    try {
        $key = Get-ItemProperty -LiteralPath $path -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $key) { return $null }
    if (-not $key.DisplayVersion) { return $null }
    return [pscustomobject]@{
        DisplayVersion = $key.DisplayVersion
        DisplayName = $key.DisplayName
        InstallLocation = $key.InstallLocation
        Publisher = $key.Publisher
        Scope = if ($Root -like "HKCU:*") { "per-user" } else { "system-wide" }
        RegistryPath = $path
    }
}

function Get-InstalledVersion {
    # Fast path: the canonical Inno Setup AppId_is1 key under each
    # of the three uninstall roots. Matches every install we've ever
    # shipped from this repo. HKLM wins over HKCU when both exist
    # (rare; happens after a per-user install followed by elevated
    # reinstall).
    foreach ($root in $UninstallRoots) {
        $info = Read-UninstallKey -Root $root -Subkey $InnoAppId
        if ($info) { return $info }
    }
    # Fallback: enumerate every uninstall subkey and match by
    # DisplayName. Covers historical installs registered under a
    # different AppId + any case where the registry provider can't
    # reach the canonical key for some other reason.
    foreach ($root in $UninstallRoots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        try {
            $children = Get-ChildItem -LiteralPath $root -ErrorAction Stop
        } catch {
            continue
        }
        foreach ($child in $children) {
            try {
                $key = Get-ItemProperty -LiteralPath $child.PSPath -ErrorAction Stop
            } catch {
                continue
            }
            if (-not $key) { continue }
            if ($key.DisplayName -and $key.DisplayName -like "*$DisplayNameMatch*") {
                if ($key.DisplayVersion) {
                    return [pscustomobject]@{
                        DisplayVersion = $key.DisplayVersion
                        DisplayName = $key.DisplayName
                        InstallLocation = $key.InstallLocation
                        Publisher = $key.Publisher
                        Scope = if ($root -like "HKCU:*") { "per-user" } else { "system-wide" }
                        RegistryPath = $child.PSPath
                    }
                }
            }
        }
    }
    return $null
}

function Write-RegistryDiagnostic {
    # Surface where we looked so the user can attach the output to
    # an issue if the lookup keeps failing. Cheap, deterministic, no
    # side effects.
    Write-Host ""
    Write-Host "Registry lookup diagnostic:" -ForegroundColor Yellow
    foreach ($root in $UninstallRoots) {
        $canonical = Join-Path -Path $root -ChildPath $InnoAppId
        $existsCanonical = Test-Path -LiteralPath $canonical
        $rootExists = Test-Path -LiteralPath $root
        $childCount = 0
        if ($rootExists) {
            try {
                $childCount = (Get-ChildItem -LiteralPath $root -ErrorAction Stop).Count
            } catch {
                $childCount = -1
            }
        }
        Write-Host ("  {0}" -f $root)
        Write-Host ("    exists: {0} (child keys: {1})" -f $rootExists, $childCount)
        Write-Host ("    canonical key {0}: {1}" -f $InnoAppId, $existsCanonical)
    }
    Write-Host ("  DisplayName fallback pattern: *{0}*" -f $DisplayNameMatch)
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
    Write-Host ("No installed Meeting Notetaker detected. Tried the " +
                "canonical Inno Setup key + a DisplayName fallback " +
                "across HKLM, HKLM\WOW6432Node, and HKCU.") `
        -ForegroundColor Yellow
    Write-RegistryDiagnostic
    Write-Host ""
    Write-Host "Use install.ps1 for a fresh install:"
    Write-Host "  iwr -useb https://raw.githubusercontent.com/$Owner/$Repo/main/install.ps1 | iex"
    Write-Host ""
    Write-Host ("If you do have an existing install, paste the above " +
                "diagnostic + the output of:") -ForegroundColor Yellow
    Write-Host ("  Get-ChildItem 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' " +
                "| Where-Object { (Get-ItemProperty `$_.PSPath -ErrorAction SilentlyContinue).DisplayName " +
                "-like '*Meeting Notetaker*' } | Select-Object -ExpandProperty PSChildName")
    Write-Host ("into a new issue on $Owner/$Repo.") -ForegroundColor Yellow
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
    Write-Host "Running silent in-place upgrade..." -ForegroundColor Cyan
    Write-Host ("Same flags as the in-app updater: /SILENT " +
                "/SUPPRESSMSGBOXES /NORESTART. Inno Setup's stable " +
                "AppId means the existing install gets replaced in " +
                "place; Windows Restart Manager closes the running " +
                "app via the installer's CloseApplications hook and " +
                "relaunches it after.")
    Write-Host ("(Use install.ps1 instead if you want the full " +
                "wizard with EULA + install-dir prompts.)") `
        -ForegroundColor DarkGray

    # Match meeting_notetaker/utils/updater.launch_installer
    # (utils/updater.py:294): /SILENT suppresses the wizard UI,
    # /SUPPRESSMSGBOXES auto-confirms any prompts, /NORESTART tells
    # Inno Setup never to request a Windows reboot. -Wait so the
    # script doesn't exit before the installer + Restart Manager
    # relaunch dance finishes.
    $proc = Start-Process -FilePath $temp `
        -ArgumentList "/SILENT","/SUPPRESSMSGBOXES","/NORESTART" `
        -Wait -PassThru
    if ($proc.ExitCode -eq 0) {
        Write-Host ""
        Write-Host "Upgrade complete." -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host ("Installer exited with code $($proc.ExitCode). " +
                    "Check the installer's log under " +
                    "%TEMP%\Setup Log*.txt for details.") `
            -ForegroundColor Yellow
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
