<#
.SYNOPSIS
    Layer D — policy hardening for a café PC. Works on Windows Home.

.DESCRIPTION
    Closes the bypasses that do not involve code: killing the agent from Task Manager,
    undoing the policy in regedit, and rebooting into safe mode.

    Windows Home has no Group Policy editor, so every policy here is applied through the
    registry instead. That is not a downgrade — the same values are what gpedit writes —
    but it does mean one extra step. The policies live in the customer's own HKCU hive,
    which a standard user can normally write to, so this script also denies that user
    write access to the policy key. Without the ACL step the customer can simply delete
    the value and get Task Manager back.

    Safe mode is handled by making the watchdog run there too, rather than by trying to
    block safe mode outright. A lock that survives safe mode is stronger than a boot path
    you hope nobody finds.

    Everything here is reversible with unharden.ps1.

.NOTES
    Elevation: REQUIRED. Run this in an ELEVATED (Administrator) PowerShell prompt.

    The target account must have logged in at least once, so that its profile hive
    exists on disk for the policies to be written into.

.EXAMPLE
    .\scripts\harden.ps1 -UserName cafe

.EXAMPLE
    .\scripts\harden.ps1 -UserName cafe -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # The customer-facing standard account. Never an admin, never the account you are
    # sitting in — both are refused below.
    [Parameter(Mandatory = $true)]
    [string]$UserName,

    [string]$ServiceName = 'PlaySlotWatchdog',

    # Leave the boot configuration alone. Useful on a dev box where you still want
    # recovery and safe mode available.
    [switch]$SkipBootHardening
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

# ---- Guards -----------------------------------------------------------------

$targetUser = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue

if (-not $targetUser) {
    throw "Local user '$UserName' does not exist. Create it first: New-LocalUser -Name $UserName"
}

# Hardening the account you are sitting in is how you lock yourself out of your own
# machine, so it is refused outright rather than warned about.
if ($UserName -eq $env:USERNAME) {
    throw "Refusing to harden '$UserName' — that is the account running this script."
}

$administrators = Get-LocalGroupMember -Group 'Administrators' -ErrorAction SilentlyContinue

if ($administrators | Where-Object { $_.SID.Value -eq $targetUser.SID.Value }) {
    throw "Refusing to harden '$UserName' — it is an Administrator. Layer D assumes a standard user."
}

$sid = $targetUser.SID.Value

$profile = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
    Where-Object { $_.SID -eq $sid }

if (-not $profile) {
    throw "'$UserName' has no profile on disk yet. Sign in as that user once, sign out, then re-run this."
}

Write-Host ''
Write-Host 'Layer D — policy hardening' -ForegroundColor Cyan
Write-Host "  User    : $UserName"
Write-Host "  SID     : $sid"
Write-Host "  Profile : $($profile.LocalPath)"
Write-Host "  Service : $ServiceName"
Write-Host ''

# ---- Load the target user's registry hive -----------------------------------

# HKCU only ever refers to the *current* user, so the customer's hive has to be reached
# through HKEY_USERS. If they are not signed in, their hive is not mounted and NTUSER.DAT
# must be loaded by hand.
$hiveWasLoaded = $false
$userRoot = "Registry::HKEY_USERS\$sid"

if (-not (Test-Path -LiteralPath $userRoot)) {
    $ntuser = Join-Path $profile.LocalPath 'NTUSER.DAT'

    if (-not (Test-Path -LiteralPath $ntuser)) {
        throw "Profile hive not found at '$ntuser'."
    }

    if ($PSCmdlet.ShouldProcess($ntuser, 'Load registry hive')) {
        & reg.exe load "HKU\$sid" $ntuser | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to load '$ntuser'. Make sure '$UserName' is signed out."
        }

        $hiveWasLoaded = $true
        Write-Host '-> Loaded profile hive' -ForegroundColor Yellow
    }
}

try {
    $policyKey = "$userRoot\Software\Microsoft\Windows\CurrentVersion\Policies\System"

    if (-not (Test-Path -LiteralPath $policyKey)) {
        if ($PSCmdlet.ShouldProcess($policyKey, 'Create policy key')) {
            New-Item -Path $policyKey -Force | Out-Null
        }
    }

    # ---- Policies -----------------------------------------------------------

    # DisableRegistryTools has to go on too, or the customer simply opens regedit and
    # deletes DisableTaskMgr. The two are only meaningful together.
    $policies = @(
        @{ Name = 'DisableTaskMgr';       Value = 1; Why = 'Task Manager (closes the kill-the-agent bypass)' }
        @{ Name = 'DisableRegistryTools'; Value = 1; Why = 'regedit (stops the policy above being undone)' }
        @{ Name = 'DisableChangePassword';Value = 1; Why = 'password change from the security screen' }
    )

    Write-Host '-> Applying policies' -ForegroundColor Yellow

    foreach ($policy in $policies) {
        if ($PSCmdlet.ShouldProcess("$policyKey\$($policy.Name)", "Set to $($policy.Value)")) {
            New-ItemProperty -Path $policyKey -Name $policy.Name -Value $policy.Value `
                -PropertyType DWord -Force | Out-Null
        }

        Write-Host "     $($policy.Name) = $($policy.Value)  — disables $($policy.Why)"
    }

    # ---- Lock the policy key against the user it applies to ------------------

    # The whole point. A standard user has write access to their own HKCU by default, so
    # without this they can delete the values above. Deny is evaluated before Allow, so
    # this beats whatever the inherited permissions grant.
    Write-Host '-> Denying the user write access to the policy key' -ForegroundColor Yellow

    if ($PSCmdlet.ShouldProcess($policyKey, 'Deny write to target user')) {
        $acl = Get-Acl -Path $policyKey

        $rule = New-Object System.Security.AccessControl.RegistryAccessRule(
            (New-Object System.Security.Principal.SecurityIdentifier($sid)),
            [System.Security.AccessControl.RegistryRights]'SetValue, CreateSubKey, Delete, ChangePermissions, TakeOwnership',
            [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Deny)

        $acl.AddAccessRule($rule)
        Set-Acl -Path $policyKey -AclObject $acl
    }
}
finally {
    # Always unload, even on failure — a hive left mounted blocks the user signing in.
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

# ---- Make the watchdog survive safe mode ------------------------------------

# Deliberately not "block safe mode". Blocking is a boot path the customer might still
# find; running the service there means safe mode simply is not a bypass.
Write-Host '-> Registering the watchdog to run in safe mode' -ForegroundColor Yellow

foreach ($variant in @('Minimal', 'Network')) {
    $safeBootKey = "HKLM:\SYSTEM\CurrentControlSet\Control\SafeBoot\$variant\$ServiceName"

    if ($PSCmdlet.ShouldProcess($safeBootKey, 'Register service for safe boot')) {
        New-Item -Path $safeBootKey -Force | Out-Null
        New-ItemProperty -Path $safeBootKey -Name '(Default)' -Value 'Service' `
            -PropertyType String -Force | Out-Null
    }

    Write-Host "     SafeBoot\$variant\$ServiceName = Service"
}

# ---- Boot configuration -----------------------------------------------------

if ($SkipBootHardening) {
    Write-Host '-> Skipping boot hardening (-SkipBootHardening)' -ForegroundColor DarkGray
}
else {
    Write-Host '-> Hardening boot configuration' -ForegroundColor Yellow

    if ($PSCmdlet.ShouldProcess('{default}', 'bcdedit /set recoveryenabled No')) {
        & bcdedit.exe /set '{default}' recoveryenabled No | Out-Null

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "bcdedit recoveryenabled failed (exit $LASTEXITCODE)."
        }
    }

    # Takes away the "Troubleshoot > Startup Settings" route into safe mode.
    if ($PSCmdlet.ShouldProcess('WinRE', 'reagentc /disable')) {
        & reagentc.exe /disable | Out-Null

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "reagentc /disable failed (exit $LASTEXITCODE). It may already be disabled."
        }
    }
}

# ---- Done -------------------------------------------------------------------

Write-Host ''
Write-Host 'Hardened.' -ForegroundColor Green
Write-Host ''
Write-Host 'Verify as the customer account (sign in as ' -NoNewline
Write-Host $UserName -ForegroundColor Yellow -NoNewline
Write-Host '):'
Write-Host '  1. Ctrl+Shift+Esc          -> "Task Manager has been disabled by your administrator"'
Write-Host '  2. regedit                 -> blocked by the same message'
Write-Host '  3. Kill the agent          -> not possible without Task Manager; watchdog restarts it anyway'
Write-Host ''
Write-Host 'Still to do by hand — these cannot be scripted:' -ForegroundColor Cyan
Write-Host '  * Set a BIOS/UEFI supervisor password.'
Write-Host '  * Disable USB and network boot in the firmware, and set the internal disk first.'
Write-Host '    Without these, the whole machine boots from a stick and every layer above is moot.'
Write-Host ''
Write-Host 'Reverse everything with:' -ForegroundColor Cyan
Write-Host "      .\scripts\unharden.ps1 -UserName $UserName" -ForegroundColor Yellow
Write-Host ''
