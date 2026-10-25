# fleet-msg: thin Windows shim for the Python bus client; forwards every argument.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = (Join-Path $here '..\src') + [IO.Path]::PathSeparator + $env:PYTHONPATH
if (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -m plangraph.fleet_bus.client @args
  exit $LASTEXITCODE
}
python -m plangraph.fleet_bus.client @args
exit $LASTEXITCODE
