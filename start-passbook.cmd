@echo off
rem Windows launcher. Double-click this file in Explorer. SPEC §22.4.
rem
rem Explorer runs a .cmd from its own folder, but a shortcut may not, so it cds
rem to itself anyway. Everything else is scripts\launch.py, shared with the
rem macOS and Linux launchers.

cd /d "%~dp0"

rem The Microsoft Store stub named `python` opens the Store instead of running
rem anything, so `py` (the real launcher, installed with Python) comes first.
set "PASSBOOK_PY="
where py >nul 2>&1 && set "PASSBOOK_PY=py -3"
if not defined PASSBOOK_PY (
    where python >nul 2>&1 && set "PASSBOOK_PY=python"
)

if not defined PASSBOOK_PY (
    echo.
    echo   Python 3 is not installed.
    echo.
    echo   Get it from https://www.python.org/downloads/windows/
    echo   Tick "Add python.exe to PATH" in the installer, then run this again.
    echo.
    pause
    exit /b 1
)

%PASSBOOK_PY% scripts\launch.py
set "PASSBOOK_STATUS=%ERRORLEVEL%"

echo.
pause
exit /b %PASSBOOK_STATUS%
