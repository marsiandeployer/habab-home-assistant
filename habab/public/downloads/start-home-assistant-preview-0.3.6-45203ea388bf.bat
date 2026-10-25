@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $installRoot=Join-Path $env:LOCALAPPDATA 'HababHome'; New-Item -ItemType Directory -Force -Path $installRoot | Out-Null; $archive=Join-Path $installRoot 'package.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://habab-demo.wpmix.net/downloads/habab-home-windows-45203ea388bf.zip' -OutFile $archive; if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne '45203ea388bfd0af33d866aa196e9f8534444eb2a5171d13260d5f4b8e67c08f') {throw 'Package SHA-256 check failed. Download the BAT again.'}; Expand-Archive -Path $archive -DestinationPath $installRoot -Force; & (Join-Path $installRoot 'start-windows.ps1'); exit $LASTEXITCODE"
set "HABAB_RESULT=%ERRORLEVEL%"
pause
exit /b %HABAB_RESULT%
