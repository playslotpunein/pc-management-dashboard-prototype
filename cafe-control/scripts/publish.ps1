<#
.SYNOPSIS
    Builds and publishes PlaySlot.Watchdog and PlaySlot.Agent as self-contained,
    single-file win-x64 executables.

.DESCRIPTION
    Output is written to the repo's publish/ folder in the layout install.ps1 expects:

        publish/
          Watchdog/  PlaySlot.Watchdog.exe + appsettings.json
          Agent/     PlaySlot.Agent.exe

    The watchdog resolves its agent path relative to its own directory
    (..\Agent\PlaySlot.Agent.exe), so this layout is what makes the default
    configuration work without editing anything.

.NOTES
    Elevation: NOT required. Run this in a normal PowerShell prompt.

.EXAMPLE
    .\scripts\publish.ps1

.EXAMPLE
    .\scripts\publish.ps1 -Configuration Debug
#>
[CmdletBinding()]
param(
    [string]$Configuration = 'Release',
    [string]$Runtime = 'win-x64',

    # Defaults to <repo>\publish. Everything is resolved from $PSScriptRoot so the
    # repo works from any location after a clone.
    [string]$OutputRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $repoRoot 'publish'
}

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw 'dotnet was not found on PATH. Install the .NET 10 SDK and reopen the prompt.'
}

$projects = @(
    @{ Name = 'Watchdog'; Path = Join-Path $repoRoot 'PlaySlot.Watchdog\PlaySlot.Watchdog.csproj' }
    @{ Name = 'Agent';    Path = Join-Path $repoRoot 'PlaySlot.Agent\PlaySlot.Agent.csproj' }
)

foreach ($project in $projects) {
    if (-not (Test-Path -LiteralPath $project.Path)) {
        throw "Project not found: $($project.Path)"
    }
}

Write-Host ''
Write-Host 'Publishing PlaySlot.CafeControl' -ForegroundColor Cyan
Write-Host "  Configuration : $Configuration"
Write-Host "  Runtime       : $Runtime"
Write-Host "  Output        : $OutputRoot"
Write-Host ''

# Clear stale output so a removed file never lingers in a deployment.
if (Test-Path -LiteralPath $OutputRoot) {
    Remove-Item -LiteralPath $OutputRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

foreach ($project in $projects) {
    $target = Join-Path $OutputRoot $project.Name

    Write-Host "-> $($project.Name)" -ForegroundColor Yellow

    & dotnet publish $project.Path `
        --configuration $Configuration `
        --runtime $Runtime `
        --self-contained true `
        -p:PublishSingleFile=true `
        --output $target

    if ($LASTEXITCODE -ne 0) {
        throw "dotnet publish failed for $($project.Name) (exit code $LASTEXITCODE)."
    }

    Write-Host ''
}

Write-Host 'Publish complete.' -ForegroundColor Green
Write-Host ''

Get-ChildItem -LiteralPath $OutputRoot -Recurse -Filter *.exe |
    Select-Object @{ Name = 'Executable'; Expression = { $_.FullName.Substring($OutputRoot.Length + 1) } },
                  @{ Name = 'Size (MB)';  Expression = { [math]::Round($_.Length / 1MB, 1) } } |
    Format-Table -AutoSize

Write-Host 'Next: run scripts\install.ps1 from an ELEVATED prompt.' -ForegroundColor Cyan
Write-Host ''
