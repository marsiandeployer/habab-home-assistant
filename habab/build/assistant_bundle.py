"""One generated assistant module shared by every public demo entrypoint."""

import hashlib
import os
from pathlib import Path

ENTRY_START = '<!-- habab-assistant-entry:start -->'
ENTRY_END = '<!-- habab-assistant-entry:end -->'
OLD_START = '<!-- habab-assistant-preview:start -->'
OLD_END = '<!-- habab-assistant-preview:end -->'
LEGACY_TAG = '<script type="module" src="/demos/home-assistant/integrations/setup-chat.js?v=5"></script>'


def assistant_release(source):
    return _release(Path(source).read_bytes())


def _release(data):
    digest = hashlib.sha256(data).hexdigest()
    relative = f'assistant/releases/{digest[:16]}/setup-chat.js'
    return {'sha256': digest, 'bytes': len(data), 'relative_path': relative,
            'module_url': '/demos/home-assistant/' + relative}


def render_entrypoint(html, module_url):
    replacement = ENTRY_START + f'<script type="module" src="{module_url}"></script>' + ENTRY_END
    families = [(start, end) for start, end in ((ENTRY_START, ENTRY_END), (OLD_START, OLD_END))
                if start in html or end in html]
    if len(families) > 1 or (families and LEGACY_TAG in html):
        raise ValueError('ambiguous assistant entrypoint')
    if families:
        start, end = families[0]
        if html.count(start) != 1 or html.count(end) != 1:
            raise ValueError('ambiguous assistant entrypoint')
        before, rest = html.split(start)
        _, after = rest.split(end)
        return before + replacement + after
    if html.count(LEGACY_TAG) != 1:
        raise ValueError('expected one assistant module')
    return html.replace(LEGACY_TAG, replacement, 1)


def publish_assistant(public_dir, source):
    public_dir = Path(public_dir)
    data = Path(source).read_bytes()
    release = _release(data)
    target = public_dir / release['relative_path']
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError('immutable assistant module differs')
    if not target.exists():
        target.write_bytes(data)
    # Cached HTML can still import this address; it forwards to the same UI.
    shim = public_dir / 'integrations/setup-chat.js'
    shim.parent.mkdir(parents=True, exist_ok=True)
    temporary = shim.with_suffix('.js.next')
    temporary.write_text('// Generated compatibility entry; source: distribution/src/habab_home/frontend/setup-chat.js.\n'
                         + f'import "{release["module_url"]}";\n', encoding='utf-8')
    os.replace(temporary, shim)
    return release
