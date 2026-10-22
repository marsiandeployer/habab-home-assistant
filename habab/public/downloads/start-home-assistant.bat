@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $installRoot=Join-Path $env:LOCALAPPDATA 'HababHome'; New-Item -ItemType Directory -Force -Path $installRoot | Out-Null; $archive=Join-Path $installRoot 'package.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://habab-demo.wpmix.net/downloads/habab-home-windows-7245ec1af7d7.zip' -OutFile $archive; if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne '7245ec1af7d760763dbcb90952084f058d284498a1bf09748fde35445bbb5a86') {throw 'Package SHA-256 check failed. Download the BAT again.'}; Expand-Archive -Path $archive -DestinationPath $installRoot -Force; & (Join-Path $installRoot 'start-windows.ps1'); exit $LASTEXITCODE"
set "HABAB_RESULT=%ERRORLEVEL%"
pause
exit /b %HABAB_RESULT%
