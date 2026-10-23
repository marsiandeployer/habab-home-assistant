@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose-assistant.ps1"
set "HABAB_RESULT=%ERRORLEVEL%"
if not "%HABAB_RESULT%"=="0" (
  echo.
  echo The assistant diagnostic did not complete. Read the message above and retry.
)
pause
exit /b %HABAB_RESULT%
