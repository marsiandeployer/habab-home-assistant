#!/usr/bin/env python3
"""Build an allowlisted, credential-free Windows kit and its one-click BAT."""
import hashlib
import io
import json
from pathlib import Path
import zipfile
from source_provenance import release_with_source, resolve_source_provenance
from windows_package_files import collect_windows_package_names, resolve_windows_package_source

root = Path(__file__).resolve().parent
output = root.parent / 'public/downloads'
output.mkdir(parents=True, exist_ok=True)
names = collect_windows_package_names(root)
buffer = io.BytesIO()
release_data = json.loads((root / 'release.json').read_text(encoding='utf-8'))
revision, dirty = resolve_source_provenance(root, release_data)
embedded_release = json.dumps(
    release_with_source(release_data, revision, dirty), indent=2
) + '\n'
with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
    for name in sorted(names):
        info = zipfile.ZipInfo(name, date_time=(2026, 10, 22, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        data = (embedded_release.encode('utf-8') if name == 'release.json'
                else resolve_windows_package_source(root, name).read_bytes())
        if name.endswith('.bat'):
            data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        bundle.writestr(info, data)
archive = buffer.getvalue()
digest = hashlib.sha256(archive).hexdigest()
filename = f'habab-home-windows-{digest[:12]}.zip'
(output / filename).write_bytes(archive)
url = 'https://habab-demo.wpmix.net/downloads/' + filename
bootstrap_prefix = (
    "$ErrorActionPreference='Stop'; "
    "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
    "$installRoot=Join-Path $env:LOCALAPPDATA 'HababHome'; "
    "New-Item -ItemType Directory -Force -Path $installRoot | Out-Null; "
    "$archive=Join-Path $installRoot 'package.zip'; "
    f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' -OutFile $archive; "
    f"if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne '{digest}') "
    "{throw 'Package SHA-256 check failed. Download the BAT again.'}; "
    "Expand-Archive -Path $archive -DestinationPath $installRoot -Force; "
)
launchers = {
    'start-home-assistant.bat': "& (Join-Path $installRoot 'start-windows.ps1'); exit $LASTEXITCODE",
    'start-xiaomi-home.bat': "& (Join-Path $installRoot 'start-windows.ps1') -XiaomiHome; exit $LASTEXITCODE",
    'support_tunnel.bat': "& (Join-Path $installRoot 'support_tunnel.ps1'); exit $LASTEXITCODE",
}
for launcher, target in launchers.items():
    command = bootstrap_prefix + target
    bat = '@echo off\r\nsetlocal\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' + command + '"\r\nset "HABAB_RESULT=%ERRORLEVEL%"\r\npause\r\nexit /b %HABAB_RESULT%\r\n'
    (output / launcher).write_bytes(bat.encode('ascii'))
(output / 'windows-package.json').write_text(json.dumps({'filename': filename, 'sha256': digest,
    'bytes': len(archive), 'files': sorted(names)}, indent=2) + '\n')
print(json.dumps({'filename': filename, 'sha256': digest, 'files': len(names), 'bytes': len(archive)}))
