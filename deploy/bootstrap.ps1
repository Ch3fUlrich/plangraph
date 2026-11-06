#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipAutoOS,
    [string]$AutoOSRoot,
    [string]$CatalogDbPath,
    [string]$ProbeHost,
    [string]$ProbeConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$pythonCommand = @('python3', 'python', 'py') | Where-Object {
    $candidate = Get-Command $_ -CommandType Application -ErrorAction SilentlyContinue
    $candidate -and $candidate.Source -notmatch '[\\/]WindowsApps[\\/]'
} | Select-Object -First 1
if (-not $pythonCommand) { throw "Python interpreter required (python3, python or py)" }

function Resolve-AutoOSRoot {
    [CmdletBinding()]
    param(
        [string]$ExplicitPath
    )

    if ($ExplicitPath -and (Test-Path -Path $ExplicitPath)) {
        return (Resolve-Path $ExplicitPath).Path
    }

    return $null
}

function Invoke-PlangraphBootstrap {
    [CmdletBinding()]
    param()

    if ($ProbeHost -or $ProbeConfig) {
        & $pythonCommand -m plangraph.spawner.probe_enroll $ProbeHost $ProbeConfig ([int]$DryRun.IsPresent)
        if ($LASTEXITCODE -ne 0) { throw "Probe enrollment failed" }
        return
    }

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

    & $pythonCommand -m plangraph.spawner.stall_watchdog tick --dry-run
    if ($LASTEXITCODE -ne 0) { throw "Watchdog count failed" }

    if (-not $SkipAutoOS -and $AutoOSRoot) {
        $resolvedAutoOS = Resolve-AutoOSRoot -ExplicitPath $AutoOSRoot
        if (-not $resolvedAutoOS) {
            throw "AutoOS repository not found: $AutoOSRoot"
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
                        throw "AutoOS setup failed with exit code $LASTEXITCODE"
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
        Write-Host "[DRY-RUN] Would execute: $pythonCommand $($pythonArgs -join ' ')"
    } else {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $pythonCommand @pythonArgs
            if ($LASTEXITCODE -ne 0) {
                throw "Catalog DB init-and-seed failed with exit code $LASTEXITCODE"
            }
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }

    $watchdogArgs = @("-m", "plangraph.spawner.stall_watchdog", "install", "--host", "windows")
    if ($DryRun) { Write-Host "[DRY-RUN] Would execute: $pythonCommand $($watchdogArgs -join ' ')" }
    else {
        & $pythonCommand @watchdogArgs
        if ($LASTEXITCODE -ne 0) { throw "Watchdog installation failed" }
    }

    # Omnigraph: site config, cache client, session environment, health verification.
    if ($DryRun) { Write-Host 'Omnigraph dry-run: read site config; cache client; install session environment; verify health' }
    else { try {
        & $pythonCommand (Join-Path $PSScriptRoot 'omnigraph_step.py') --dry-run ([int]$DryRun.IsPresent)
        if ($LASTEXITCODE -ne 0) { Write-Host 'Omnigraph skipped: helper unavailable' }
    } catch { Write-Host 'Omnigraph skipped: helper unavailable' } }

    Write-Host "Plangraph bootstrap completed successfully."
}

Invoke-PlangraphBootstrap
