@echo off
rem Update passbook. Double-click this file in Explorer.
rem
rem The app itself cannot do this: applying an update rebuilds a container
rem image, which needs the Docker socket, and the one process that listens on a
rem port and parses uploaded files is the last place that socket belongs. So
rem the Status page tells you an update exists and this runs it.
rem
rem Everything happens inside WSL, because that is where the stack runs.

cd /d "%~dp0.."

echo.
echo   Updating passbook. This backs up first, then pulls, rebuilds and
echo   checks the ledger. Leave this window open until it finishes.
echo.

wsl.exe --cd "%CD%" -- bash -lc "make update"
set RESULT=%ERRORLEVEL%

echo.
if %RESULT% neq 0 (
    echo   Update stopped. Nothing was half-applied — the version you had is
    echo   still the one running, and your backup is in backups\.
    echo.
    echo   The message above says what stopped it.
) else (
    echo   Done. Open http://localhost:8081
)
echo.
pause
