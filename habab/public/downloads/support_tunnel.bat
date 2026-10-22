@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $installRoot=Join-Path $env:LOCALAPPDATA 'HababHome'; New-Item -ItemType Directory -Force -Path $installRoot | Out-Null; $archive=Join-Path $installRoot 'package.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://habab-demo.wpmix.net/downloads/habab-home-windows-6e5c8315ce86.zip' -OutFile $archive; if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne '6e5c8315ce8631b4f46e8a7261a616d11364adaaba0dc54f22a78638a28c70b6') {throw 'Package SHA-256 check failed. Download the BAT again.'}; Expand-Archive -Path $archive -DestinationPath $installRoot -Force; & (Join-Path $installRoot 'support_tunnel.ps1'); exit $LASTEXITCODE"
set "HABAB_RESULT=%ERRORLEVEL%"
pause
exit /b %HABAB_RESULT%
