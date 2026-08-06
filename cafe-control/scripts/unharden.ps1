<#
.SYNOPSIS
    Reverses harden.ps1 — restores Task Manager, regedit, safe mode and recovery.

.DESCRIPTION
    Undoes every change harden.ps1 makes: removes the policy values, removes the Deny
    ACL that stopped the user editing them, unregisters the watchdog from safe boot, and
    puts the boot configuration back.

    Run this on a dev machine before you need Task Manager again, and on a café PC before
    handing the hardware to anyone else.

    The account itself is left alone. Removing a user destroys their profile, which is
    not something a script called "unharden" should do quietly — delete it by hand if you
    actually want it gone.

.NOTES
    Elevation: REQUIRED. Run this in an ELEVATED (Administrator) PowerShell prompt.

    The target account must be signed out, so its hive can be loaded.

.EXAMPLE
    .\scripts\unharden.ps1 -UserName cafe
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$UserName,

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
    Write-Host "      $PSCommandPath -UserName $UserName" -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

$targetUser = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue

if (-not $targetUser) {
    throw "Local user '$UserName' does not exist."
}

$sid = $targetUser.SID.Value

Write-Host ''
Write-Host 'Reversing Layer D hardening' -ForegroundColor Cyan
Write-Host "  User    : $UserName"
Write-Host "  SID     : $sid"
Write-Host ''

# ---- Load the target user's hive --------------------------------------------

$hiveWasLoaded = $false
$userRoot = "Registry::HKEY_USERS\$sid"

if (-not (Test-Path -LiteralPath $userRoot)) {
    $profile = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
        Where-Object { $_.SID -eq $sid }

    if ($profile) {
        $ntuser = Join-Path $profile.LocalPath 'NTUSER.DAT'

        if ((Test-Path -LiteralPath $ntuser) -and
            $PSCmdlet.ShouldProcess($ntuser, 'Load registry hive')) {

            & reg.exe load "HKU\$sid" $ntuser | Out-Null

            if ($LASTEXITCODE -ne 0) {
                throw "Failed to load '$ntuser'. Make sure '$UserName' is signed out."
            }

            $hiveWasLoaded = $true
            Write-Host '-> Loaded profile hive' -ForegroundColor Yellow
        }
    }
}

try {
    $policyKey = "$userRoot\Software\Microsoft\Windows\CurrentVersion\Policies\System"

    if (Test-Path -LiteralPath $policyKey) {

        # The Deny ACL comes off first. Leave it in place and the value removals below
        # fail, because Deny applies to this process too once ownership is involved.
        Write-Host '-> Removing the Deny rule' -ForegroundColor Yellow

        if ($PSCmdlet.ShouldProcess($policyKey, 'Remove Deny ACL')) {
            $acl = Get-Acl -Path $policyKey
            $userSid = New-Object System.Security.Principal.SecurityIdentifier($sid)

            $removed = 0

            foreach ($rule in @($acl.Access)) {
                if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny -and
                    $rule.IdentityReference.Value -eq $userSid.Value) {

                    [void]$acl.RemoveAccessRule($rule)
                    $removed++
                }
            }

            if ($removed -gt 0) {
                Set-Acl -Path $policyKey -AclObject $acl
                Write-Host "     removed $removed rule(s)"
            }
            else {
                Write-Host '     none found' -ForegroundColor DarkGray
            }
        }

        Write-Host '-> Removing policy values' -ForegroundColor Yellow

        foreach ($name in @('DisableTaskMgr', 'DisableRegistryTools', 'DisableChangePassword')) {
            $exists = Get-ItemProperty -Path $policyKey -Name $name -ErrorAction SilentlyContinue

            if ($exists) {
                if ($PSCmdlet.ShouldProcess("$policyKey\$name", 'Remove value')) {
                    Remove-ItemProperty -Path $policyKey -Name $name -Force
                }

                Write-Host "     $name removed"
            }
            else {
                Write-Host "     $name not set" -ForegroundColor DarkGray
            }
        }
    }
    else {
        Write-Host '-> No policy key present; nothing to undo' -ForegroundColor DarkGray
    }
}
finally {
    if ($hiveWasLoaded) {
        [gc]::Collect()
        [gc]::WaitForPendingFinalizers()

        & reg.exe unload "HKU\$sid" | Out-Null

        if ($LASTEXITCODE -eq 0) {
            Write-Host '-> Unloaded profile hive' -ForegroundColor Yellow
        }
        else {
            Write-Warning "Could not unload HKU\$sid. It will clear on reboot."
        }
    }
}

# ---- Safe boot registration -------------------------------------------------

Write-Host '-> Unregistering the watchdog from safe boot' -ForegroundColor Yellow

foreach ($variant in @('Minimal', 'Network')) {
    $safeBootKey = "HKLM:\SYSTEM\CurrentControlSet\Control\SafeBoot\$variant\$ServiceName"

    if (Test-Path -LiteralPath $safeBootKey) {
        if ($PSCmdlet.ShouldProcess($safeBootKey, 'Remove safe boot registration')) {
            Remove-Item -Path $safeBootKey -Recurse -Force
        }

        Write-Host "     SafeBoot\$variant removed"
    }
    else {
        Write-Host "     SafeBoot\$variant not registered" -ForegroundColor DarkGray
    }
}

# ---- Boot configuration -----------------------------------------------------

Write-Host '-> Restoring boot configuration' -ForegroundColor Yellow

if ($PSCmdlet.ShouldProcess('{default}', 'bcdedit /set recoveryenabled Yes')) {
    & bcdedit.exe /set '{default}' recoveryenabled Yes | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "bcdedit recoveryenabled failed (exit $LASTEXITCODE)."
    }
}

if ($PSCmdlet.ShouldProcess('WinRE', 'reagentc /enable')) {
    & reagentc.exe /enable | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "reagentc /enable failed (exit $LASTEXITCODE). Check: reagentc /info"
    }
}

Write-Host ''
Write-Host 'Reversed.' -ForegroundColor Green
Write-Host ''
Write-Host "Task Manager and regedit are available to '$UserName' again after their next sign-in."
Write-Host ''
Write-Host 'Not reversed — undo these by hand if you set them:' -ForegroundColor Cyan
Write-Host '  * BIOS/UEFI supervisor password'
Write-Host '  * Firmware boot order and USB boot'
Write-Host "  * The '$UserName' account itself (delete it manually if you want it gone)"
Write-Host ''
