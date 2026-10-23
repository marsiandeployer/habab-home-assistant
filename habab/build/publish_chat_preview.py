"""Publish the opt-in onboarding preview without rebuilding the HA demo."""

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'distribution/src/habab_home/frontend'
PUBLIC = ROOT / 'public'
ORIGINAL = '<script type="module" src="/demos/home-assistant/integrations/setup-chat.js?v=5"></script>'
START = '<!-- habab-assistant-preview:start -->'
END = '<!-- habab-assistant-preview:end -->'


def render_entrypoint(html, module_url):
    loader = (
        START + '<script type="module">'
        'const preview=new URLSearchParams(location.search).get("habab-preview")==="onboarding";'
        'if(preview)document.documentElement.dataset.hababDemoPreview="onboarding";'
        f'import(preview?{json.dumps(module_url)}:'
        '"/demos/home-assistant/integrations/setup-chat.js?v=5");'
        '</script>' + END
    )
    if START in html or END in html:
        if html.count(START) != 1 or html.count(END) != 1:
            raise ValueError('ambiguous preview loader')
        before, rest = html.split(START)
        _, after = rest.split(END)
        return before + loader + after
    if html.count(ORIGINAL) != 1:
        raise ValueError('expected the single retained public chat module')
    return html.replace(ORIGINAL, loader, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', help='Publish the opt-in preview files and loader')
    args = parser.parse_args()
    files = {'setup-chat.js': (SOURCE / 'setup-chat.js').read_bytes()}
    helper = SOURCE / 'demo-onboarding.js'
    if helper.exists():
        files[helper.name] = helper.read_bytes()
    digest = hashlib.sha256(b''.join(name.encode() + b'\0' + data for name, data in sorted(files.items()))).hexdigest()
    relative = Path('previews/assistant') / digest[:16]
    module_url = '/' + (relative / 'setup-chat.js').as_posix()
    index = PUBLIC / 'index.html'
    before = index.read_text(encoding='utf-8')
    after = render_entrypoint(before, module_url)
    if args.publish:
        destination = PUBLIC / relative
        destination.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            target = destination / name
            if target.exists() and target.read_bytes() != data:
                raise ValueError('immutable preview artifact differs')
            if not target.exists():
                target.write_bytes(data)
        next_index = PUBLIC / '.index.preview.next'
        next_index.write_text(after, encoding='utf-8')
        os.replace(next_index, index)
    print(json.dumps({
        'published': args.publish, 'bundle_sha256': digest, 'module_url': module_url,
        'preview_url': 'https://habab-demo.wpmix.net/?habab-preview=onboarding',
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        'entrypoint_before_sha256': hashlib.sha256(before.encode()).hexdigest(),
        'entrypoint_after_sha256': hashlib.sha256(after.encode()).hexdigest(),
    }, indent=2))


if __name__ == '__main__':
    main()
