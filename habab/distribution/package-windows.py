#!/usr/bin/env python3
"""Build the credential-free Windows payload and record its compiled EXE."""
import argparse
import hashlib
import io
import json
import os
import re
from pathlib import Path
import zipfile
from urllib.parse import urlsplit
from source_provenance import release_with_source, resolve_source_provenance
from windows_package_files import collect_windows_package_names, resolve_windows_package_source

INSTALLER_FILENAME = 'habab_home_assistant.exe'


def public_base_url(value: str) -> str:
    """Require a public HTTPS URL that is safe inside the PowerShell bootstrap."""
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"https://[A-Za-z0-9._~:/+-]+", value)):
        raise ValueError('HABAB_WINDOWS_PUBLIC_BASE must be a public HTTPS URL without credentials, query or fragment')
    return value.rstrip('/')


def package_windows(root: Path, output: Path, public_base: str) -> dict:
    public_base = public_base_url(public_base)
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
    url = public_base + '/' + filename
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
    metadata = {'filename': filename, 'sha256': digest,
        'bytes': len(archive), 'files': sorted(names), 'version': release_data['version'],
        'source_revision': revision, 'source_dirty': dirty,
        'public_base': public_base}
    (output / 'windows-package.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'filename': filename, 'sha256': digest, 'files': len(names), 'bytes': len(archive)}))
    return metadata


def record_installer(output: Path, compiler_version: str) -> dict:
    """Add checksum/provenance only after the Windows compiler produced an EXE."""
    metadata_path = output / 'windows-package.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if (metadata.get('source_dirty') != 'false'
            or not re.fullmatch(r'[0-9a-f]{40,64}', metadata.get('source_revision', ''))):
        raise ValueError('Public Windows installers require a known, clean source revision')
    executable = (output / INSTALLER_FILENAME).read_bytes()
    if not executable.startswith(b'MZ'):
        raise ValueError('Windows installer is not a PE executable')
    metadata['installer'] = {
        'filename': INSTALLER_FILENAME,
        'sha256': hashlib.sha256(executable).hexdigest(),
        'bytes': len(executable),
        'source_revision': metadata['source_revision'],
        'source_dirty': False,
        'compiler': {'name': 'Inno Setup', 'version': compiler_version},
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    return metadata['installer']


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=root.parent / 'public/downloads')
    parser.add_argument('--record-installer', action='store_true')
    parser.add_argument('--compiler-version', default='')
    args = parser.parse_args()
    if args.record_installer:
        print(json.dumps(record_installer(args.output_dir, args.compiler_version)))
    else:
        package_windows(root, args.output_dir, os.environ.get(
            'HABAB_WINDOWS_PUBLIC_BASE', 'https://habab-demo.wpmix.net/downloads'))


if __name__ == '__main__':
    main()
