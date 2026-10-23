@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $installRoot=Join-Path $env:LOCALAPPDATA 'HababHome'; New-Item -ItemType Directory -Force -Path $installRoot | Out-Null; $archive=Join-Path $installRoot 'package.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://habab-demo.wpmix.net/downloads/habab-home-windows-53913a9b700b.zip' -OutFile $archive; if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne '53913a9b700b17d859873ac58954db045a3247d75501cfd07b8a29b4a3ab58b0') {throw 'Package SHA-256 check failed. Download the BAT again.'}; Expand-Archive -Path $archive -DestinationPath $installRoot -Force; & (Join-Path $installRoot 'start-windows.ps1'); exit $LASTEXITCODE"
set "HABAB_RESULT=%ERRORLEVEL%"
pause
exit /b %HABAB_RESULT%
