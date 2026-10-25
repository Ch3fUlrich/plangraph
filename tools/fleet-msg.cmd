@echo off
rem fleet-msg: thin Windows shim for the Python bus client.
rem
rem Quoting contract: the argument tail is captured exactly once, into the
rem quoted set below, and never onto a command line.  cmd re-parses argument
rem expansions as shell syntax, so a tail forwarded onto a command line lets
rem an embedded ampersand, pipe, redirection or caret become a command.  The
rem fixed bootstrap below receives no user text; it splits the capture with
rem the Windows argv rules (CommandLineToArgvW), the exact inverse of the
rem caller's quoting, and runs the client module with the interpreter
rem isolated from ambient paths.
rem Limitation: cmd's own parser cannot quote a tail holding unbalanced
rem double quotes; pass such text through fleet-msg.ps1 or as JSON.
setlocal DisableDelayedExpansion
set "FLEET_MSG_ARGV=%*"
set "FLEET_MSG_HOME=%~dp0"
where py >nul 2>nul
if errorlevel 1 goto :plain
py -3 -I -c "import os,runpy,sys;from ctypes import POINTER,byref,c_wchar_p,wintypes,windll;sys.path.insert(0,os.path.join(os.environ.get('FLEET_MSG_HOME',''),'..','src'));raw='fleet-msg '+os.environ.get('FLEET_MSG_ARGV','');C=windll.shell32.CommandLineToArgvW;C.argtypes=[c_wchar_p,POINTER(wintypes.INT)];C.restype=POINTER(c_wchar_p);n=wintypes.INT();p=C(raw,byref(n));a=[p[i] for i in range(n.value)];windll.kernel32.LocalFree(p);sys.argv=a;runpy.run_module('plangraph.fleet_bus.client',run_name='__main__')"
exit /b %errorlevel%
:plain
python -I -c "import os,runpy,sys;from ctypes import POINTER,byref,c_wchar_p,wintypes,windll;sys.path.insert(0,os.path.join(os.environ.get('FLEET_MSG_HOME',''),'..','src'));raw='fleet-msg '+os.environ.get('FLEET_MSG_ARGV','');C=windll.shell32.CommandLineToArgvW;C.argtypes=[c_wchar_p,POINTER(wintypes.INT)];C.restype=POINTER(c_wchar_p);n=wintypes.INT();p=C(raw,byref(n));a=[p[i] for i in range(n.value)];windll.kernel32.LocalFree(p);sys.argv=a;runpy.run_module('plangraph.fleet_bus.client',run_name='__main__')"
exit /b %errorlevel%
