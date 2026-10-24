#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipAutoOS,
    [string]$AutoOSRoot,
    [string]$CatalogDbPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-AutoOSRoot {
    [CmdletBinding()]
    param(
        [string]$ExplicitPath
    )

    if ($ExplicitPath -and (Test-Path -Path $ExplicitPath)) {
        return (Resolve-Path $ExplicitPath).Path
    }

    if ($env:AUTOOS_ROOT -and (Test-Path -Path $env:AUTOOS_ROOT)) {
        return (Resolve-Path $env:AUTOOS_ROOT).Path
    }

    $candidates = @(
        (Join-Path $PSScriptRoot "..\..\AutoOS"),
        (Join-Path $PSScriptRoot "..\..\..\AutoOS"),
        (Join-Path $HOME "Documents\Code\AutoOS"),
        (Join-Path $HOME ".cache\plangraph\AutoOS")
    )

    foreach ($cand in $candidates) {
        if (Test-Path -Path (Join-Path $cand "setup.ps1")) {
            return (Resolve-Path $cand).Path
        }
    }

    return $null
}

function Invoke-PlangraphBootstrap {
    [CmdletBinding()]
    param()

    $reqPath = Join-Path $PSScriptRoot "requirements-host.json"
    if (-not (Test-Path $reqPath)) {
        Write-Error "Host requirements file not found: $reqPath"
        return
    }

    $reqJson = Get-Content -Path $reqPath -Raw | ConvertFrom-Json
    $coreList = @($reqJson.autoos_components.core)
    $agentList = @($reqJson.autoos_components.agents)
    $allComponents = $coreList + $agentList

    Write-Host "Resolved host components ($($allComponents.Count)): $($allComponents -join ', ')"

    $resolvedAutoOS = Resolve-AutoOSRoot -ExplicitPath $AutoOSRoot

    if (-not $SkipAutoOS) {
        if (-not $resolvedAutoOS) {
            Write-Warning "AutoOS repository not found. Provide -AutoOSRoot or set AUTOOS_ROOT environment variable."
        } else {
            $setupPs1 = Join-Path $resolvedAutoOS "setup.ps1"
            Write-Host "Invoking AutoOS setup at: $setupPs1"
            $onlyArg = $allComponents -join ","
            $setupArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $setupPs1, "-Only", $onlyArg, "-Yes")
            if ($DryRun) {
                $setupArgs += "-DryRun"
                Write-Host "[DRY-RUN] Would execute: powershell.exe $($setupArgs -join ' ')"
            } else {
                $prevEap = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try {
                    & powershell.exe @setupArgs
                    if ($LASTEXITCODE -ne 0) {
                        Write-Error "AutoOS setup failed with exit code $LASTEXITCODE"
                    }
                } finally {
                    $ErrorActionPreference = $prevEap
                }
            }
        }
    }

    Write-Host "Initializing Plangraph spawner catalog database..."
    $pythonArgs = @("-m", "plangraph.spawner.catalog_db", "init-and-seed")
    if ($CatalogDbPath) {
        $pythonArgs += @("--db-path", $CatalogDbPath)
    }

    if ($DryRun) {
        Write-Host "[DRY-RUN] Would execute: python $($pythonArgs -join ' ')"
    } else {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & python @pythonArgs
            if ($LASTEXITCODE -ne 0) {
                Write-Error "Catalog DB init-and-seed failed with exit code $LASTEXITCODE"
            }
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }

    $watchdogArgs = @("-m", "plangraph.spawner.stall_watchdog", "install", "--host", "windows")
    if ($DryRun) { $watchdogArgs += "--dry-run" }
    & python @watchdogArgs
    if ($LASTEXITCODE -ne 0) { throw "Watchdog installation failed" }

    Write-Host "Plangraph bootstrap completed successfully."
}

Invoke-PlangraphBootstrap
