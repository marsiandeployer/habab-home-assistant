#!/usr/bin/env python3
"""Build an allowlisted, credential-free Windows kit and its one-click BAT."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent
output = root.parent / 'public/downloads'
output.mkdir(parents=True, exist_ok=True)
names = ['Dockerfile', 'docker-entrypoint.sh', 'habab-ha.py', 'compose.windows.yaml',
         'start-windows.bat', 'start-windows.ps1', 'install-opencode-volume.py',
         'opencode-release.json', 'README-WINDOWS.md', 'LICENSE', 'NOTICE',
         'config/configuration.yaml']
names += [str(p.relative_to(root)).replace('\\', '/') for p in (root / 'src/habab_home').rglob('*')
          if p.is_file() and p.suffix in {'.py', '.js', '.json'} and '__pycache__' not in p.parts]
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
    for name in sorted(names):
        info = zipfile.ZipInfo(name, date_time=(2026, 10, 22, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        data = (root / name).read_bytes()
        if name.endswith('.bat'):
            data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        bundle.writestr(info, data)
archive = buffer.getvalue()
digest = hashlib.sha256(archive).hexdigest()
filename = f'habab-home-windows-{digest[:12]}.zip'
(output / filename).write_bytes(archive)
url = 'https://habab-demo.wpmix.net/downloads/' + filename
command = (
    "$ErrorActionPreference='Stop'; "
    "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
    "$installRoot=Join-Path $env:LOCALAPPDATA 'HababHome'; "
    "New-Item -ItemType Directory -Force -Path $installRoot | Out-Null; "
    "$archive=Join-Path $installRoot 'package.zip'; "
    f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' -OutFile $archive; "
    f"if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne '{digest}') "
    "{throw 'Package SHA-256 check failed. Download the BAT again.'}; "
    "Expand-Archive -Path $archive -DestinationPath $installRoot -Force; "
    "& (Join-Path $installRoot 'start-windows.ps1'); exit $LASTEXITCODE"
)
bat = '@echo off\r\nsetlocal\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' + command + '"\r\nset "HABAB_RESULT=%ERRORLEVEL%"\r\npause\r\nexit /b %HABAB_RESULT%\r\n'
(output / 'start-home-assistant.bat').write_bytes(bat.encode('ascii'))
(output / 'windows-package.json').write_text(json.dumps({'filename': filename, 'sha256': digest,
    'bytes': len(archive), 'files': sorted(names)}, indent=2) + '\n')
print(json.dumps({'filename': filename, 'sha256': digest, 'files': len(names), 'bytes': len(archive)}))
