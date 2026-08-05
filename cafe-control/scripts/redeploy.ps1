<#
.SYNOPSIS
    Uninstall, publish, install — the full loop in one command.

.DESCRIPTION
    The normal edit/test cycle. The uninstall step is tolerant of the service not
    being installed yet, so this works as a first-time install too.

.NOTES
    Elevation: REQUIRED. Run this in an ELEVATED (Administrator) PowerShell prompt.
    (publish.ps1 on its own does not need elevation, but install/uninstall do.)

.EXAMPLE
    .\scripts\redeploy.ps1

.EXAMPLE
    .\scripts\redeploy.ps1 -Configuration Debug
#>
[CmdletBinding()]
param(
    [string]$Configuration = 'Release',
    [string]$InstallRoot = 'C:\Program Files\PlaySlot',
    [string]$ServiceName = 'PlaySlotWatchdog'
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

$uninstall = Join-Path $PSScriptRoot 'uninstall.ps1'
$publish = Join-Path $PSScriptRoot 'publish.ps1'
$install = Join-Path $PSScriptRoot 'install.ps1'

foreach ($script in @($uninstall, $publish, $install)) {
    if (-not (Test-Path -LiteralPath $script)) {
        throw "Missing script: $script"
    }
}

Write-Host ''
Write-Host '=== 1/3  UNINSTALL ===' -ForegroundColor Magenta

& $uninstall -InstallRoot $InstallRoot -ServiceName $ServiceName -StopAgent

Write-Host '=== 2/3  PUBLISH ===' -ForegroundColor Magenta

& $publish -Configuration $Configuration

Write-Host '=== 3/3  INSTALL ===' -ForegroundColor Magenta

& $install -InstallRoot $InstallRoot -ServiceName $ServiceName

Write-Host 'Redeploy complete.' -ForegroundColor Green
Write-Host ''
