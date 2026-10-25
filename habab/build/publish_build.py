"""Copy and scope the official Home Assistant demo production build."""

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from assistant_bundle import publish_assistant, render_entrypoint
from demo_service_worker import publish_retirement_worker

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'demo/dist'
TARGET = ROOT / 'public'
RETIREMENT_WORKER = ROOT / 'build/retire-demo-service-worker.js'
UPSTREAM = ROOT.parent
STAGE = ROOT / 'tmp/ha-frontend-build/publish-stage'
RELEASE = os.environ.get('HABAB_DEMO_RELEASE', '20261021-ui5')
if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', RELEASE):
    raise ValueError('Invalid HABAB_DEMO_RELEASE')
PREFIX = f'/demos/home-assistant/releases/{RELEASE}/'


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
    if html.count('<title>Home Assistant Demo</title>') != 1:
        raise ValueError('Pinned demo title changed; review Habab branding')
    html = html.replace('<title>Home Assistant Demo</title>', '<title>Habab Home</title>')
    for quote in ('"', "'"):
        for directory in ('static', 'frontend_latest', 'frontend_es5'):
            for old in ('./..//', './/', './', '/'):
                html = html.replace(f'{quote}{old}{directory}/', f'{quote}{PREFIX}{directory}/')
        for old in ('./manifest.json', '/manifest.json'):
            html = html.replace(f'{quote}{old}{quote}', f'{quote}{PREFIX}manifest.json{quote}')
    html = html.replace('<head>', f'<head><meta name="habab-demo-release" content="{RELEASE}">')
    html = html.replace('<html', '<html data-habab-demo="true"', 1)
    return html.replace('</body>', '<script src="/demos/home-assistant/integrations/telegram-setup.js?v=20261023-1" defer></script><script type="module" src="/demos/home-assistant/integrations/setup-chat.js?v=5"></script></body>')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-published', action='store_true',
                        help='Repackage the retained flat compiler output without recompiling')
    parser.add_argument('--entrypoint', type=Path,
                        help='Explicit retained flat index for a repeat repackage')
    args = parser.parse_args()
    if (TARGET / 'releases' / RELEASE).exists():
        raise SystemExit('Release already exists; choose a new HABAB_DEMO_RELEASE')
    source_root = TARGET if args.from_published else SOURCE
    if not (source_root / 'index.html').is_file():
        raise SystemExit(f'Upstream build is missing: {source_root}')
    source_html = (args.entrypoint or source_root / 'index.html').read_text(encoding='utf-8')
    if args.from_published and '/releases/' in source_html:
        raise SystemExit('Already repackaged; rebuild from the pinned upstream source')
    if STAGE.exists():
        shutil.rmtree(STAGE)
    shutil.copytree(source_root, STAGE, ignore=shutil.ignore_patterns('releases'))
    (TARGET / 'integrations').mkdir(exist_ok=True)
    assistant = publish_assistant(TARGET, ROOT / 'distribution/src/habab_home/frontend/setup-chat.js')
    if not args.from_published:
        shutil.copy2(UPSTREAM / 'LICENSE.md', STAGE / 'LICENSE.md')
    (STAGE / 'ATTRIBUTION.md').write_text(
        '# Home Assistant frontend demo\n\n'
        'This interactive demo is compiled from the official Home Assistant frontend '
        'repository at commit `92599a88037ef5b7bd822efd7301028d824f7f5b`. '
        'The upstream project is licensed under Apache License 2.0; see [LICENSE.md](LICENSE.md). '
        'Habab changes: Russian demo labels and native Russian locale; subpath-safe, self-hosted assets; '
        'bounded demo-state persistence with visible API error handling; and the origin-wide service worker is disabled. '
        'The demo runs mock states only and does not connect to Home Assistant Core or devices. '
        'This build is not an official Home Assistant service or endorsement.\n',
        encoding='utf-8',
    )

    index = STAGE / 'index.html'
    html = render_entrypoint(scope_html(source_html), assistant['module_url'])
    index.write_text(html, encoding='utf-8')

    manifest = STAGE / 'manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding='utf-8'))
        data['name'] = 'Habab Home'
        data['short_name'] = 'Habab Home'
        data['start_url'] = '/demos/home-assistant/?homescreen=1'
        for icon in data.get('icons', []):
            icon['src'] = './' + icon['src'].lstrip('/')
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
    TARGET.mkdir(parents=True, exist_ok=True)
    for source in STAGE.rglob('*'):
        if not source.is_file() or source.name == 'index.html':
            continue
        relative = source.relative_to(STAGE)
        destination = TARGET / 'releases' / RELEASE / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    # Switch the entrypoint only after every referenced asset is in place.
    publish_retirement_worker(TARGET, RETIREMENT_WORKER)
    next_index = TARGET / '.index.html.next'
    shutil.copy2(STAGE / 'index.html', next_index)
    os.replace(next_index, TARGET / 'index.html')

    print(f'published {TARGET}')
    print(f'files={sum(p.is_file() for p in TARGET.rglob("*"))}')
    print('old hashed assets retained; index.html replaced atomically last')


if __name__ == '__main__':
    main()
