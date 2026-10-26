"""Copy and scope the official Home Assistant demo production build."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from assistant_bundle import publish_assistant, render_entrypoint
from demo_service_worker import publish_retirement_worker

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'demo/dist'
TARGET = ROOT / 'public'
RETIREMENT_WORKER = ROOT / 'build/retire-demo-service-worker.js'
UPSTREAM = ROOT.parent
STAGE = ROOT / 'tmp/ha-frontend-build/publish-stage'
PUBLISHED_ASSET_DIRS = ('assets', 'frontend_es5', 'frontend_latest', 'static', 'stub_config')
PUBLISHED_ASSET_FILES = ('_headers', 'manifest.json', 'LICENSE.md')
PUBLISHED_ENTRYPOINT = ROOT / 'build/flat-entrypoint.html'
BRAND_ASSET_DIR = ROOT / 'build/brand-assets'
BRAND_ASSETS = (
    'habab.svg',
    'habab-mask.svg',
    'habab.ico',
    'habab-32.png',
    'habab-192.png',
    'habab-512.png',
    'habab-maskable-512.png',
    'apple-touch-icon.png',
)
UPSTREAM_BASELINE = '92599a88037ef5b7bd822efd7301028d824f7f5b'
RELEASE = os.environ.get('HABAB_DEMO_RELEASE', '20261021-ui5')
if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', RELEASE):
    raise ValueError('Invalid HABAB_DEMO_RELEASE')
PREFIX = f'/demos/home-assistant/releases/{RELEASE}/'


def scope_demo_realtime_headers(headers_text):
    """Allow the demo Realtime relay through the fallback CSP."""
    return headers_text.replace(
        "connect-src 'self';",
        "connect-src 'self' wss://habab-voice.wpmix.net;",
    )


def scope_demo_inspector_headers(headers_text):
    """Allow the opt-in Dev Inspector in the demo CSP."""
    return (
        headers_text.replace(
            "script-src 'self' 'unsafe-inline';",
            "script-src 'self' 'unsafe-inline' https://op.wpmix.net;",
        )
        .replace(
            "connect-src 'self' wss://habab-voice.wpmix.net;",
            "connect-src 'self' https://op.wpmix.net wss://habab-voice.wpmix.net;",
        )
        .replace(
            "manifest-src 'self'; object-src 'none';",
            "manifest-src 'self'; frame-src 'self' https://op.wpmix.net; object-src 'none';",
        )
    )


def stage_published_frontend(source_root, stage, entrypoint, *, license_source=None):
    """Copy only the retained flat frontend inputs, excluding adjacent public artifacts."""
    for name in PUBLISHED_ASSET_DIRS:
        source = source_root / name
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f'Missing compiled frontend directory: {name}')
        if any(path.is_symlink() for path in source.rglob('*')):
            raise ValueError(f'Symlink in compiled frontend directory: {name}')
    asset_sources = {}
    for name in PUBLISHED_ASSET_FILES:
        source = license_source if name == 'LICENSE.md' and license_source is not None else source_root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Missing compiled frontend file: {name}')
        asset_sources[name] = source
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise ValueError('Flat frontend entrypoint is missing')

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        for name in PUBLISHED_ASSET_DIRS:
            shutil.copytree(source_root / name, stage / name)
        for name in PUBLISHED_ASSET_FILES:
            shutil.copy2(asset_sources[name], stage / name)
        shutil.copy2(entrypoint, stage / 'index.html')
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def install_brand_assets(stage):
    """Copy the checked-in Habab icons into the staged frontend."""
    destination = stage / 'static' / 'habab'
    for name in BRAND_ASSETS:
        source = BRAND_ASSET_DIR / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Missing Habab brand asset: {name}')
    if destination.is_symlink():
        raise ValueError('Compiled frontend static/habab is a symlink')
    if destination.exists():
        if not destination.is_dir():
            raise ValueError('Compiled frontend static/habab is not a directory')
        for name in BRAND_ASSETS:
            source = BRAND_ASSET_DIR / name
            installed = destination / name
            if installed.is_symlink() or not installed.is_file():
                raise ValueError(f'Incomplete staged Habab brand assets: {name}')
            if source.read_bytes() != installed.read_bytes():
                raise ValueError(f'Conflicting staged Habab brand asset: {name}')
        return
    destination.mkdir()
    try:
        for name in BRAND_ASSETS:
            shutil.copy2(BRAND_ASSET_DIR / name, destination / name)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def brand_demo_manifest(data):
    """Set the demo PWA identity and icon set to the Habab brand."""
    data['name'] = 'Habab Home Assistant'
    data['short_name'] = 'Habab Home'
    data['start_url'] = '/demos/home-assistant/?homescreen=1'
    data['description'] = 'Демо-интерфейс Habab Home Assistant для умного дома.'
    data['theme_color'] = '#155e4b'
    data['icons'] = [
        {
            'src': './static/habab/habab-192.png',
            'sizes': '192x192',
            'type': 'image/png',
            'purpose': 'any',
        },
        {
            'src': './static/habab/habab-512.png',
            'sizes': '512x512',
            'type': 'image/png',
            'purpose': 'any',
        },
        {
            'src': './static/habab/habab-maskable-512.png',
            'sizes': '512x512',
            'type': 'image/png',
            'purpose': 'maskable',
        },
    ]
    return data


def scope_javascript(content, *, legacy=False):
    """Scope both DOM-relative legacy chunks and demo media to this release."""
    for old in ('./..//frontend_latest/', './..//frontend_es5/',
                './/frontend_latest/', './/frontend_es5/'):
        content = content.replace(old, './')
    for quote in ('"', "'", '`'):
        for directory in ('assets', 'static'):
            content = content.replace(f'{quote}/{directory}/', f'{quote}{PREFIX}{directory}/')
    if legacy:
        # Webpack's script loader resolves this against the document, unlike
        # modern ES-module imports, which resolve against the importing file.
        content = content.replace('.p="./"', f'.p="{PREFIX}frontend_es5/"')
    # These are the pinned demo's synthetic map fixtures, not real users.
    if 'timeline:[[30,' in content and 'id:"arsaboo"' in content:
        for english, russian in {'Home': 'Дом', 'Work': 'Работа', 'Gym': 'Спортзал',
                                 'School': 'Школа', 'Supermarket': 'Магазин', 'Car': 'Автомобиль'}.items():
            content = content.replace(f'"{english}"', json.dumps(russian, ensure_ascii=False))
    marker = '"component.light.entity_component._.state.on":"Включено"'
    if marker in content and '"component.person.entity_component._.state.home"' not in content:
        content = content.replace(marker,
            '"component.person.entity_component._.state.home":"Дома",'
            '"component.person.entity_component._.state.not_home":"Вне дома",'
            '"component.light.entity_component._.state_attributes.brightness.name":"Яркость",' + marker)
    content = content.replace(
        '.postMessage({type:"habab-ha-demo-interaction"},location.origin)',
        '.postMessage({type:"habab-ha-demo-interaction"},"https://habab.ru")',
    )
    # The persisted Habab fixture is one home. Do not invite visitors into
    # unrelated upstream example profiles with different entity IDs/locales.
    content = re.sub(
        r' <ha-control-button-group slot="features"> <ha-control-button \.disabled=\$\{this\._switching\} @click=\$\{this\._nextConfig\}> \$\{this\.hass\.localize\("ui\.panel\.page-demo\.cards\.demo\.next_demo"\)\} </ha-control-button> </ha-control-button-group>',
        '', content,
    )
    if 'https://tile.openstreetmap.org/{z}/{x}/{y}.png' in content:
        content = content.replace('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                                  '/demos/home-assistant/map-tiles/{z}/{x}/{y}.png')
        # Use the native Leaflet raster engine. The vector engine otherwise
        # requires third-party TileJSON, glyphs and workers at runtime.
        if content.count('if(!l&&s()){') != 1:
            raise ValueError('Pinned map engine changed; review raster selection')
        content = content.replace('if(!l&&s()){', 'if(false){')
        if content.count('rasterOnly:this._forceLeaflet') != 1:
            raise ValueError('Pinned native map adapter changed; review raster selection')
        content = content.replace('rasterOnly:this._forceLeaflet', 'rasterOnly:!0')
    return re.sub(r'\n?//# sourceMappingURL=[^\s]+', '', content)


def scope_html(html):
    upstream_title = '<title>Home Assistant Demo</title>'
    habab_title = '<title>Habab Home Assistant</title>'
    if html.count(upstream_title) == 1 and html.count(habab_title) == 0:
        html = html.replace(upstream_title, habab_title)
    elif html.count(upstream_title) != 0 or html.count(habab_title) != 1:
        raise ValueError('Pinned demo title changed; review Habab branding')
    for quote in ('"', "'"):
        for directory in ('static', 'frontend_latest', 'frontend_es5'):
            for old in ('./..//', './/', './', '/'):
                html = html.replace(f'{quote}{old}{directory}/', f'{quote}{PREFIX}{directory}/')
        for old in ('./manifest.json', '/manifest.json'):
            html = html.replace(f'{quote}{old}{quote}', f'{quote}{PREFIX}manifest.json{quote}')
    html = html.replace('<head>', f'<head><meta name="habab-demo-release" content="{RELEASE}">')
    html = html.replace('<html', '<html data-habab-demo="true"', 1)
    return html.replace('</body>', '<script src="/demos/home-assistant/integrations/telegram-setup.js?v=20261023-1" defer></script><script type="module" src="/demos/home-assistant/integrations/setup-chat.js?v=5"></script></body>')


def source_revision(value):
    if value is not None and not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Frontend source revision must be a full lowercase Git SHA-1')
    return value


def validate_source_options(*, from_published, source_dir, entrypoint, frontend_revision):
    if from_published and source_dir:
        raise ValueError('--from-published and --source-dir are mutually exclusive')
    if source_dir and entrypoint:
        raise ValueError('--entrypoint cannot be combined with --source-dir')
    if frontend_revision and not source_dir:
        raise ValueError('--frontend-source-revision requires --source-dir')
    revision = source_revision(frontend_revision)
    if source_dir and not revision:
        raise ValueError('--source-dir requires --frontend-source-revision')
    return revision


def attribution_text(frontend_revision):
    fork_revision = frontend_revision or 'unknown'
    return (
        '# Home Assistant frontend demo\n\n'
        f'This demo uses the Habab frontend fork at source revision `{fork_revision}`. '
        f'Its Home Assistant upstream baseline is commit `{UPSTREAM_BASELINE}`. '
        'The upstream project is licensed under Apache License 2.0; see [LICENSE.md](LICENSE.md). '
        'Habab changes: Russian demo labels and native Russian locale; subpath-safe, self-hosted assets; '
        'bounded demo-state persistence with visible API error handling; and the origin-wide service worker is disabled. '
        'The demo runs mock states only and does not connect to Home Assistant Core or devices. '
        'This build is not an official Home Assistant service or endorsement.\n'
    )


def write_provenance(stage, frontend_revision):
    provenance = {
        'frontend_source_revision': frontend_revision,
        'home_assistant_upstream_baseline': UPSTREAM_BASELINE,
    }
    (stage / 'BUILD-PROVENANCE.json').write_text(
        json.dumps(provenance, indent=2) + '\n', encoding='utf-8')


def set_static_permissions(root):
    """Make a copied static tree readable by the public web server."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Static release root is missing or is a symlink')
    directories = []
    files = []
    invalid = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in dirnames:
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                invalid.append(path)
        for name in filenames:
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                invalid.append(path)
            else:
                files.append(path)
    if invalid:
        raise ValueError('Unexpected symlink or special file in static release')
    for path in files:
        os.chmod(path, 0o644, follow_symlinks=False)
    for path in reversed(directories):
        os.chmod(path, 0o755, follow_symlinks=False)


def ensure_public_directory(path):
    """Create one public ancestor if needed and make it traversable."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Public directory is a symlink')
    if not path.exists():
        path.mkdir()
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError('Public directory is not a directory')
    os.chmod(path, 0o755, follow_symlinks=False)


def copy_public_release(stage, release_root, public_root):
    """Copy immutable assets, then set final destination modes before index swap."""
    public_root = Path(public_root)
    release_root = Path(release_root)
    releases_root = public_root / 'releases'
    if release_root.parent != releases_root:
        raise ValueError('Release directory must be a direct child of public/releases')
    ensure_public_directory(public_root)
    ensure_public_directory(releases_root)
    if release_root.exists() or release_root.is_symlink():
        raise ValueError('Immutable release directory already exists')
    release_root.mkdir()
    for source in stage.rglob('*'):
        if not source.is_file() or source.name == 'index.html':
            continue
        relative = source.relative_to(stage)
        destination = release_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    set_static_permissions(release_root)


def replace_public_index(stage_index, target):
    next_index = target / '.index.html.next'
    if next_index.is_symlink():
        raise ValueError('Temporary public index is a symlink')
    shutil.copy2(stage_index, next_index)
    os.chmod(next_index, 0o644, follow_symlinks=False)
    os.replace(next_index, target / 'index.html')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-published', action='store_true',
                        help='Repackage the retained flat compiler output without recompiling')
    parser.add_argument('--source-dir', type=Path,
                        help='Use an external compiled frontend output directory')
    parser.add_argument('--frontend-source-revision',
                        help='Full Git SHA-1 of the source used for the compiled frontend')
    parser.add_argument('--entrypoint', type=Path,
                        help='Explicit retained flat index for a repeat repackage')
    args = parser.parse_args()
    try:
        frontend_revision = validate_source_options(
            from_published=args.from_published,
            source_dir=args.source_dir,
            entrypoint=args.entrypoint,
            frontend_revision=args.frontend_source_revision,
        )
    except ValueError as err:
        raise SystemExit(str(err)) from err
    if (TARGET / 'releases' / RELEASE).exists():
        raise SystemExit('Release already exists; choose a new HABAB_DEMO_RELEASE')
    source_root = TARGET if args.from_published else args.source_dir or SOURCE
    if args.source_dir and (source_root.is_symlink() or not source_root.is_dir()):
        raise SystemExit('Compiled frontend source directory is missing or is a symlink')
    if not (source_root / 'index.html').is_file():
        raise SystemExit(f'Upstream build is missing: {source_root}')
    entrypoint = args.entrypoint or (PUBLISHED_ENTRYPOINT if args.from_published else source_root / 'index.html')
    source_html = entrypoint.read_text(encoding='utf-8')
    if args.from_published and '/releases/' in source_html:
        raise SystemExit('Already repackaged; rebuild from the pinned upstream source')
    if args.from_published or args.source_dir:
        try:
            stage_published_frontend(
                source_root, STAGE, entrypoint,
                license_source=UPSTREAM / 'LICENSE.md' if args.source_dir else None,
            )
        except ValueError as err:
            raise SystemExit(str(err)) from err
    else:
        if STAGE.exists():
            shutil.rmtree(STAGE)
        shutil.copytree(source_root, STAGE, ignore=shutil.ignore_patterns('releases'))
    (TARGET / 'integrations').mkdir(exist_ok=True)
    assistant = publish_assistant(TARGET, ROOT / 'distribution/src/habab_home/frontend/setup-chat.js')
    if not args.from_published and not args.source_dir:
        shutil.copy2(UPSTREAM / 'LICENSE.md', STAGE / 'LICENSE.md')
    install_brand_assets(STAGE)
    (STAGE / 'ATTRIBUTION.md').write_text(attribution_text(frontend_revision), encoding='utf-8')
    write_provenance(STAGE, frontend_revision)

    index = STAGE / 'index.html'
    html = render_entrypoint(scope_html(source_html), assistant['module_url'])
    index.write_text(html, encoding='utf-8')

    manifest = STAGE / 'manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding='utf-8'))
        brand_demo_manifest(data)
        manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    headers_file = STAGE / '_headers'
    headers_text = headers_file.read_text(encoding='utf-8')
    headers_text = headers_text.replace(
        'Content-Security-Policy: form-action https:',
        "Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
        "connect-src 'self'; worker-src 'self' blob:; media-src 'self' blob: data:; "
        "manifest-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'self' https://habab.ru https://www.habab.ru",
    )
    headers_text = scope_demo_realtime_headers(headers_text)
    headers_text = scope_demo_inspector_headers(headers_text)
    headers_file.write_text(headers_text, encoding='utf-8')

    # Upstream source maps embed raw.githubusercontent.com sources. They are
    # debugging-only and must not be shipped or cause any browser source fetch.
    for source_map in STAGE.rglob('*.map'):
        source_map.unlink()
    for bundle in STAGE.rglob('*.js'):
        content = bundle.read_text(encoding='utf-8')
        content = scope_javascript(content, legacy='frontend_es5' in bundle.parts)
        bundle.write_text(content, encoding='utf-8')

    # The standalone demo has no need for a service worker. It must not claim
    # the origin root when hosted under the product subpath.
    for name in ('sw-modern.js', 'sw-legacy.js'):
        (STAGE / name).unlink(missing_ok=True)

    # Keep the currently served release intact while publishing. New bundles
    # have content-hashed names; retaining previous bundles also lets already
    # open clients finish lazy imports after index.html changes.
    release_root = TARGET / 'releases' / RELEASE
    copy_public_release(STAGE, release_root, TARGET)

    # Switch the entrypoint only after every referenced asset is in place.
    publish_retirement_worker(TARGET, RETIREMENT_WORKER)
    replace_public_index(STAGE / 'index.html', TARGET)

    # Publication has completed. Retain the new release and the last one;
    # failed builds never reach this hook.
    retention = Path(os.environ.get('RELEASE_RETENTION_TOOL', '/root/tools/release-retention/retention.py'))
    state = Path(os.environ.get('HABAB_DEMO_RETENTION_STATE', '/root/tools/release-retention/states/habab-demo.json'))
    if retention.is_file() and state.is_file():
        subprocess.run([sys.executable, str(retention), '--state', str(state),
                        '--release', RELEASE, '--artifact', RELEASE, '--apply'], check=True)
    else:
        print('release retention SKIP: initialize current/previous manifest on this host')

    print(f'published {TARGET}')
    print(f'files={sum(p.is_file() for p in TARGET.rglob("*"))}')
    print('index.html replaced atomically; release retention processed')


if __name__ == '__main__':
    main()
