<#
.SYNOPSIS
    Installs PlaySlot.Watchdog as a Windows service with automatic restart on failure.

.DESCRIPTION
    Copies the published output to the install root, registers the service as
    LocalSystem with automatic start, configures recovery actions, and starts it.

    LocalSystem is required, not just convenient: launching the agent into the
    interactive session calls WTSQueryUserToken, which needs the SE_TCB_NAME
    privilege that only LocalSystem holds.

.NOTES
    Elevation: REQUIRED. Run this in an ELEVATED (Administrator) PowerShell prompt.
    Run scripts\publish.ps1 first.

.EXAMPLE
    .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -InstallRoot 'D:\PlaySlot'
#>
[CmdletBinding()]
param(
    # The one absolute path in the repo, kept here so it can be overridden.
    [string]$InstallRoot = 'C:\Program Files\PlaySlot',

    [string]$ServiceName = 'PlaySlotWatchdog',

    # Deliberately neutral — nothing that advertises what it does.
    [string]$DisplayName = 'Session Integrity Service',

    [string]$Description = 'Maintains interactive session integrity and service availability for managed units.',

    # Defaults to <repo>\publish.
    [string]$PublishRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- Elevation check --------------------------------------------------------

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)

if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ''
    Write-Host 'This script must run elevated.' -ForegroundColor Red
    Write-Host ''
    Write-Host '  Open PowerShell as Administrator (right-click > Run as administrator),'
    Write-Host '  then run this script again:'
    Write-Host ''
    Write-Host "      $PSCommandPath" -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

# ---- Paths ------------------------------------------------------------------

$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $PublishRoot) {
    $PublishRoot = Join-Path $repoRoot 'publish'
}

$watchdogSource = Join-Path $PublishRoot 'Watchdog'
$agentSource = Join-Path $PublishRoot 'Agent'

if (-not (Test-Path -LiteralPath (Join-Path $watchdogSource 'PlaySlot.Watchdog.exe'))) {
    throw "Published watchdog not found under '$PublishRoot'. Run scripts\publish.ps1 first."
}

if (-not (Test-Path -LiteralPath (Join-Path $agentSource 'PlaySlot.Agent.exe'))) {
    throw "Published agent not found under '$PublishRoot'. Run scripts\publish.ps1 first."
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($existing) {
    throw "Service '$ServiceName' is already installed. Run scripts\uninstall.ps1 first, or use scripts\redeploy.ps1."
}

Write-Host ''
Write-Host 'Installing PlaySlot watchdog service' -ForegroundColor Cyan
Write-Host "  Service   : $ServiceName"
Write-Host "  Display   : $DisplayName"
Write-Host "  Install to: $InstallRoot"
Write-Host ''

# ---- Copy files -------------------------------------------------------------

Write-Host '-> Copying files' -ForegroundColor Yellow

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

Copy-Item -LiteralPath $watchdogSource -Destination $InstallRoot -Recurse -Force
Copy-Item -LiteralPath $agentSource -Destination $InstallRoot -Recurse -Force

$watchdogExe = Join-Path $InstallRoot 'Watchdog\PlaySlot.Watchdog.exe'

if (-not (Test-Path -LiteralPath $watchdogExe)) {
    throw "Copy succeeded but '$watchdogExe' is missing."
}

# ---- Register the service ---------------------------------------------------

Write-Host '-> Creating service' -ForegroundColor Yellow

# sc.exe parses "key= value" as two tokens: the space AFTER the '=' is required and
# there must be no space before it.
& sc.exe create $ServiceName binPath= $watchdogExe start= auto obj= LocalSystem DisplayName= $DisplayName

if ($LASTEXITCODE -ne 0) {
    throw "sc.exe create failed (exit code $LASTEXITCODE)."
}

# Windows PowerShell strips embedded quotes when forwarding an argument to a native
# executable, so however the command line is escaped, sc.exe ends up storing the path
# unquoted. An unquoted ImagePath containing spaces is the unquoted-service-path
# weakness: the SCM probes C:\Program.exe before C:\Program Files\... Writing the value
# straight to the registry sidesteps shell quoting entirely.
$serviceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"

Set-ItemProperty -Path $serviceKey -Name ImagePath -Value "`"$watchdogExe`"" -Type ExpandString

$storedPath = (Get-ItemProperty -Path $serviceKey -Name ImagePath).ImagePath

if (-not $storedPath.StartsWith('"')) {
    throw "ImagePath is still unquoted after correction: $storedPath"
}

& sc.exe description $ServiceName $Description

if ($LASTEXITCODE -ne 0) {
    Write-Warning "sc.exe description failed (exit code $LASTEXITCODE)."
}

# ---- Recovery ---------------------------------------------------------------

# Restart 5s after each of the first three failures; reset the failure counter daily.
# Without this, Windows takes no action when the service dies, and killing it once is
# enough to defeat the whole thing.
Write-Host '-> Configuring recovery' -ForegroundColor Yellow

& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000

if ($LASTEXITCODE -ne 0) {
    throw "sc.exe failure failed (exit code $LASTEXITCODE)."
}

# Treat any non-zero exit as a failure, not just a hard crash. Easy to miss, and
# without it a clean-but-unexpected exit bypasses the recovery actions above.
& sc.exe failureflag $ServiceName 1

if ($LASTEXITCODE -ne 0) {
    Write-Warning "sc.exe failureflag failed (exit code $LASTEXITCODE)."
}

# ---- Start ------------------------------------------------------------------

Write-Host '-> Starting service' -ForegroundColor Yellow

& sc.exe start $ServiceName

if ($LASTEXITCODE -ne 0) {
    throw "sc.exe start failed (exit code $LASTEXITCODE). Check Event Viewer > Windows Logs > Application."
}

Start-Sleep -Seconds 3

# ---- Verify -----------------------------------------------------------------

Write-Host ''
Write-Host 'Installed.' -ForegroundColor Green
Write-Host ''

$service = Get-Service -Name $ServiceName
Write-Host "  Status : $($service.Status)"

# Print the stored ImagePath so the binPath quoting above is visibly correct rather
# than merely assumed.
$config = & sc.exe qc $ServiceName
$binaryLine = $config | Where-Object { $_ -match 'BINARY_PATH_NAME' }

if ($binaryLine) {
    Write-Host "  $($binaryLine.Trim())"
}

Write-Host ''
Write-Host 'Verify the agent landed on the interactive desktop:' -ForegroundColor Cyan
Write-Host '  A "PlaySlot.Agent (test stub)" console window should appear within ~30s.'
Write-Host '  Its "Session Id" line must read 1 or higher. A 0 means Session 0 — wrong.'
Write-Host ''
Write-Host '  Or check without the window:' -ForegroundColor Cyan
Write-Host '      Get-Process PlaySlot.Agent | Select-Object Id, SessionId, ProcessName'
Write-Host ''
