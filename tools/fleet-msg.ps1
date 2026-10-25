# fleet-msg: thin Windows shim for the Python bus client.
#
# Quoting contract: PowerShell hands this script an argument vector, and
# @args forwards that vector untouched -- no re-parsing, so metacharacters
# and embedded quotes in a message stay literal.  The interpreter runs with
# -P so an untrusted working directory can never shadow the real client on
# sys.path; the checkout's src is pinned first through PYTHONPATH here.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = (Join-Path $here '..\src') + [IO.Path]::PathSeparator + $env:PYTHONPATH
if (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -P -m plangraph.fleet_bus.client @args
  exit $LASTEXITCODE
}
python -P -m plangraph.fleet_bus.client @args
exit $LASTEXITCODE
