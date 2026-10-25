@echo off
rem fleet-msg: thin Windows shim for the Python bus client; forwards every argument.
setlocal
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
where py >nul 2>nul
if errorlevel 1 goto :plain
py -3 -m plangraph.fleet_bus.client %*
exit /b %errorlevel%
:plain
python -m plangraph.fleet_bus.client %*
