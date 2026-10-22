@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-windows.ps1"
set "HABAB_RESULT=%ERRORLEVEL%"
if not "%HABAB_RESULT%"=="0" (
  echo.
  echo Setup did not finish. Read the message above, then run this BAT again.
)
pause
exit /b %HABAB_RESULT%
