# tools/mcp-supervise.ps1: persistent supervisor loop for MCP servers.
#
# Survives host-wide python.exe kills (e.g. taskkill /F /IM python.exe):
# pwsh hosts the restart loop and relaunches the supervisor daemon.
param(
    [string]$Config = ""
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Resolve-Path (Join-Path $here "..")
$env:PYTHONPATH = (Join-Path $repo "src") + [IO.Path]::PathSeparator + $env:PYTHONPATH

if (-not $Config) {
    $Config = Join-Path $repo "examples\mcp-supervise.workstation.json"
}

while ($true) {
    Write-Host "[mcp-supervise] Starting supervisor with config $Config"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m plangraph.mcp_supervise run --config $Config
    } else {
        python -m plangraph.mcp_supervise run --config $Config
    }
    $rc = $LASTEXITCODE
    Write-Host "[mcp-supervise] Process exited with code $rc; restarting in 2 seconds..."
    Start-Sleep -Seconds 2
}
