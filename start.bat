@echo off
rem Salesforce Ticket Investigator: set up (first time) and start. Windows: double-click this file.
rem The one file to run. No org at hand? Choose Try with the demo orgs on the page (or: start.bat --demo).
setlocal
cd /d "%~dp0"
title Salesforce Ticket Investigator

set "PY="
set "NEW_ENOUGH=import sys; raise SystemExit(0 if sys.version_info[:2] > (3, 10) else 1)"
py -3 -c "%NEW_ENOUGH%" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "%NEW_ENOUGH%" >nul 2>nul && set "PY=python"
if defined PY goto run

echo.
echo  Python 3.11 or newer was not found on this computer.
echo.
echo   1. Install it from https://www.python.org/downloads/
echo      and tick "Add python.exe to PATH" on the first screen of the installer.
echo   2. Double-click this file again.
echo.
echo  The download page opens in your browser now.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:run
%PY% scripts\bootstrap.py %*
set "STATUS=%ERRORLEVEL%"
if "%STATUS%"=="0" exit /b 0
echo.
pause
exit /b %STATUS%
