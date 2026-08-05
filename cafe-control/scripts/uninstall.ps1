<#
.SYNOPSIS
    Stops and removes the PlaySlot watchdog service and deletes its install folder.

.DESCRIPTION
    Safe to run when the service is not installed — missing pieces are reported and
    skipped rather than treated as errors.

    Any running agent is left alone by default; pass -StopAgent to kill it too. Once
    the watchdog is gone nothing will restart it, so a stray agent would otherwise
    keep running until the machine reboots.

.NOTES
    Elevation: REQUIRED. Run this in an ELEVATED (Administrator) PowerShell prompt.

.EXAMPLE
    .\scripts\uninstall.ps1

.EXAMPLE
    .\scripts\uninstall.ps1 -StopAgent
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\Program Files\PlaySlot',
    [string]$ServiceName = 'PlaySlotWatchdog',
    [string]$AgentProcessName = 'PlaySlot.Agent',

    # Also kill any running agent. Without this it survives the uninstall.
    [switch]$StopAgent
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

Write-Host ''
Write-Host 'Uninstalling PlaySlot watchdog service' -ForegroundColor Cyan
Write-Host ''

# ---- Stop and delete the service --------------------------------------------

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($service) {
    if ($service.Status -ne 'Stopped') {
        Write-Host '-> Stopping service' -ForegroundColor Yellow

        & sc.exe stop $ServiceName | Out-Null

        # Poll rather than assume: sc.exe stop returns immediately, and sc.exe delete
        # on a still-running service only marks it for deletion.
        $deadline = (Get-Date).AddSeconds(30)

        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $service.Refresh()

            if ($service.Status -eq 'Stopped') {
                break
            }
        }

        if ($service.Status -ne 'Stopped') {
            Write-Warning "Service did not stop within 30s (status: $($service.Status)). Deleting anyway; a reboot may be needed."
        }
    }

    Write-Host '-> Deleting service' -ForegroundColor Yellow

    & sc.exe delete $ServiceName | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "sc.exe delete failed (exit code $LASTEXITCODE)."
    }
}
else {
    Write-Host "-> Service '$ServiceName' is not installed; skipping" -ForegroundColor DarkGray
}

# ---- Stop the agent ---------------------------------------------------------

$agents = @(Get-Process -Name $AgentProcessName -ErrorAction SilentlyContinue)

if ($agents.Count -gt 0) {
    if ($StopAgent) {
        Write-Host "-> Stopping $($agents.Count) agent process(es)" -ForegroundColor Yellow
        $agents | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    else {
        Write-Warning "$($agents.Count) agent process(es) still running. Re-run with -StopAgent to kill them."
    }
}

# ---- Remove the install folder ----------------------------------------------

if (Test-Path -LiteralPath $InstallRoot) {
    # Sanity check before a recursive delete: only remove a folder that actually looks
    # like our install, so a mistyped -InstallRoot cannot take out something else.
    $looksLikeOurInstall =
        (Test-Path -LiteralPath (Join-Path $InstallRoot 'Watchdog\PlaySlot.Watchdog.exe')) -or
        (Test-Path -LiteralPath (Join-Path $InstallRoot 'Agent\PlaySlot.Agent.exe'))

    if ($looksLikeOurInstall) {
        Write-Host '-> Removing install folder' -ForegroundColor Yellow

        try {
            Remove-Item -LiteralPath $InstallRoot -Recurse -Force
        }
        catch {
            Write-Warning "Could not fully remove '$InstallRoot': $($_.Exception.Message)"
            Write-Warning 'A file is probably still locked. Remove it manually after a reboot.'
        }
    }
    else {
        Write-Warning "'$InstallRoot' does not contain the expected executables; leaving it alone."
    }
}
else {
    Write-Host "-> '$InstallRoot' does not exist; skipping" -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Uninstalled.' -ForegroundColor Green
Write-Host ''
