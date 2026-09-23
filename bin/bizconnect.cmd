@echo off
rem biz-connect command shim for cmd / PowerShell:  bizconnect <service> <verb> [args]
rem Runs scripts\bizconnect.py (the self-bootstrapping launcher) with the first working
rem Python 3.9+: %BIZCONNECT_PYTHON%, then py -3, python, python3. Each candidate is
rem test-run, so the Windows Store "python" alias (not a real Python) is skipped.
rem No labels/goto on purpose: this file must work with LF or CRLF line endings.
setlocal
set "BC_SCRIPT=%~dp0..\scripts\bizconnect.py"
set "BC_CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
set "BC_CMD="
if defined BIZCONNECT_PYTHON set BC_CMD="%BIZCONNECT_PYTHON%"
if not defined BC_CMD py -3 -c "%BC_CHECK%" >nul 2>nul && set "BC_CMD=py -3"
if not defined BC_CMD python -c "%BC_CHECK%" >nul 2>nul && set "BC_CMD=python"
if not defined BC_CMD python3 -c "%BC_CHECK%" >nul 2>nul && set "BC_CMD=python3"
if not defined BC_CMD echo bizconnect: no Python 3.9+ found (tried py -3, python, python3). 1>&2
if not defined BC_CMD echo Install Python 3 from https://www.python.org/downloads/ and re-run, or set BIZCONNECT_PYTHON. 1>&2
if not defined BC_CMD exit /b 9009
%BC_CMD% "%BC_SCRIPT%" %*
exit /b %errorlevel%
