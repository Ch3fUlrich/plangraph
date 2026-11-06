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
        $probeCode = @'
import os, pathlib, re, shlex, stat, sys, tempfile
host, name, dry = sys.argv[1:]
valid = lambda h: re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", h) is not None
assert valid(host) and name, "paired probe host/config required; invalid host"
path = pathlib.Path(name)
mode = path.stat().st_mode
assert path.is_file() and not mode & 0o077, "config must exist and be private"
text = path.read_bytes().decode("utf-8")
lines = text.splitlines(keepends=True)
entries = [(i, line.split("=", 1)[1].strip()) for i, line in enumerate(lines)
           if re.match(r"^\s*PLANGRAPH_PROBE_HOSTS\s*=", line)]
assert len(entries) == 1, "exactly one hosts assignment required"
index, value = entries[0]
hosts = shlex.split(value)
if len(hosts) == 1: hosts = hosts[0].split()
assert all(valid(h) for h in hosts), "invalid configured host"
if host in hosts: sys.exit(0)
if dry == "1": print("Would add probe host " + host); sys.exit(0)
lines[index] = "PLANGRAPH_PROBE_HOSTS=" + shlex.quote(" ".join(hosts + [host])) + "\n"
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
try:
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), stat.S_IMODE(mode)); stream.write("".join(lines).encode("utf-8"))
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
'@
        & python -c $probeCode $ProbeHost $ProbeConfig ([int]$DryRun.IsPresent)
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

    & python -m plangraph.spawner.stall_watchdog tick --dry-run
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
        Write-Host "[DRY-RUN] Would execute: python $($pythonArgs -join ' ')"
    } else {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & python @pythonArgs
            if ($LASTEXITCODE -ne 0) {
                throw "Catalog DB init-and-seed failed with exit code $LASTEXITCODE"
            }
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }

    $watchdogArgs = @("-m", "plangraph.spawner.stall_watchdog", "install", "--host", "windows")
    if ($DryRun) { Write-Host "[DRY-RUN] Would execute: python $($watchdogArgs -join ' ')" }
    else {
        & python @watchdogArgs
        if ($LASTEXITCODE -ne 0) { throw "Watchdog installation failed" }
    }

    Write-Host "Plangraph bootstrap completed successfully."
}

Invoke-PlangraphBootstrap
