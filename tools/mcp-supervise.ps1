# tools/mcp-supervise.ps1: persistent supervisor loop for MCP servers.
#
# Survives host-wide python.exe kills (e.g. taskkill /F /IM python.exe):
# pwsh hosts the restart loop and relaunches the supervisor daemon.
#
# One instance per host (named mutex): a second start exits 0 with a message, so a
# scheduled-task retrigger cannot stack loops that fight over the same ports.
# A config that dies within MinUptime seconds FailLimit times in a row (bad config,
# missing runtime) is retried every BackoffDelay seconds instead of RestartDelay.
param(
    [string]$Config = "",
    [string]$MutexName = 'Global\plangraph-mcp-supervise',
    [int]$RestartDelay = 2,
    [int]$BackoffDelay = 60,
    [int]$FailLimit = 5,
    [int]$MinUptime = 30
)

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # The previous holder was killed without releasing; the mutex is ours now.
    $owned = $true
}
if (-not $owned) {
    Write-Host "[mcp-supervise] already running (mutex $MutexName is held); exiting"
    $mutex.Dispose()
    exit 0
}

try {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $repo = Resolve-Path (Join-Path $here "..")
    $env:PYTHONPATH = (Join-Path $repo "src") + [IO.Path]::PathSeparator + $env:PYTHONPATH

    if (-not $Config) {
        $Config = Join-Path $repo "examples\mcp-supervise.workstation.json"
    }

    $fastFailures = 0
    while ($true) {
        Write-Host "[mcp-supervise] Starting supervisor with config $Config"
        $started = Get-Date
        if (Get-Command py -ErrorAction SilentlyContinue) {
            py -3 -m plangraph.mcp_supervise run --config $Config
        } else {
            python -m plangraph.mcp_supervise run --config $Config
        }
        $rc = $LASTEXITCODE
        $uptime = [int]((Get-Date) - $started).TotalSeconds
        if ($uptime -lt $MinUptime) { $fastFailures++ } else { $fastFailures = 0 }
        $delay = if ($fastFailures -ge $FailLimit) { $BackoffDelay } else { $RestartDelay }
        Write-Host "[mcp-supervise] Process exited with code $rc after ${uptime}s; restarting in $delay seconds..."
        Start-Sleep -Seconds $delay
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
