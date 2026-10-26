"""Select the source files included in the Windows installer kit."""

from pathlib import Path


_WINDOWS_PACKAGE_FILES = (
    'Dockerfile', '.dockerignore', 'docker-entrypoint.sh', 'habab-ha.py', 'compose.windows.yaml',
    'start-windows.bat', 'start-windows.ps1', 'ensure-xiaomi-callback.ps1',
    'start-xiaomi-home.bat', 'install-xiaomi-home.py', 'xiaomi-home-release.json',
    'support_tunnel.bat', 'support_tunnel.ps1', 'support-relay.json',
    'diagnose-assistant.bat', 'diagnose-assistant.ps1', 'diagnose-assistant.py',
    'install-native-frontend.py', 'native-frontend-release.json',
    'install-opencode-volume.py', 'opencode-release.json', 'release.json', 'README-WINDOWS.md', 'LICENSE', 'NOTICE',
    'source_provenance.py', 'config/configuration.yaml',
    'product-tunnel/control.ps1', 'product-tunnel/connect.ps1', 'product-tunnel/home.example.json',
)
_EXTERNAL_PRODUCT_TUNNEL_FILES = frozenset({
    'product-tunnel/control.ps1',
    'product-tunnel/connect.ps1',
    'product-tunnel/home.example.json',
})
_WINDOWS_SOURCE_DIRECTORIES = (
    'src/habab_home', 'src/habab_telegram', 'src/habab_yandex_speech',
    'src/idigital_sms', 'telegram-agent',
)
_WINDOWS_SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.json', '.svg', '.md', '.vtt'}
_EXCLUDED_DIRECTORY_NAMES = {'__pycache__', 'node_modules', 'test', 'tests'}


def collect_windows_package_names(root: Path) -> list[str]:
    """Return the explicit Windows kit files and allowed distribution sources."""
    names = list(_WINDOWS_PACKAGE_FILES)
    for directory in _WINDOWS_SOURCE_DIRECTORIES:
        names.extend(
            path.relative_to(root).as_posix()
            for path in (root / directory).rglob('*')
            if path.is_file()
            and path.suffix in _WINDOWS_SOURCE_SUFFIXES
            and not _EXCLUDED_DIRECTORY_NAMES.intersection(path.parts)
        )
    # Docker copies this one integration resource; do not widen this allowlist
    # to every YAML file.
    names.append('src/idigital_sms/services.yaml')
    return sorted(names)


def resolve_windows_package_source(root: Path, name: str) -> Path:
    """Resolve package members, allowing only the reviewed adjacent tunnel files."""
    member = Path(name)
    if member.is_absolute() or '\\' in name or '..' in member.parts:
        raise ValueError(f'Unsafe Windows package member path: {name}')
    if name in _EXTERNAL_PRODUCT_TUNNEL_FILES:
        source = root.parent / member
    else:
        source = root / member
    if not source.is_file() or source.is_symlink():
        raise ValueError(f'Windows package source is missing or not a regular file: {name}')
    return source
