"""One generated assistant module shared by every public demo entrypoint."""

import hashlib
import os
import re
from pathlib import Path

ENTRY_START = '<!-- habab-assistant-entry:start -->'
ENTRY_END = '<!-- habab-assistant-entry:end -->'
OLD_START = '<!-- habab-assistant-preview:start -->'
OLD_END = '<!-- habab-assistant-preview:end -->'
LEGACY_TAG = '<script type="module" src="/demos/home-assistant/integrations/setup-chat.js?v=5"></script>'


_STATIC_IMPORT = re.compile(
    r"(?:\bimport\s*(?:[^'\"]*?\sfrom\s*)?|\bexport\s+[^'\"]*?\sfrom\s*)['\"]([^'\"]+)['\"]"
)
_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _local_module_closure(source):
    """Return source and every relative module it imports, preserving paths."""
    source = Path(source).resolve()
    root = source.parent
    modules = {}
    pending = [source]
    while pending:
        module = pending.pop()
        relative = module.relative_to(root)
        if relative in modules:
            continue
        content = module.read_bytes()
        modules[relative] = content
        text = content.decode('utf-8')
        imports = _STATIC_IMPORT.findall(text) + _DYNAMIC_IMPORT.findall(text)
        for specifier in imports:
            if not specifier.startswith(('./', '../')):
                continue
            dependency = (module.parent / specifier.split('?', 1)[0].split('#', 1)[0]).resolve()
            if not dependency.is_relative_to(root):
                raise ValueError('assistant import escapes frontend source directory')
            if not dependency.is_file():
                raise ValueError(f'missing assistant import: {specifier}')
            pending.append(dependency)
    return modules


def assistant_release(source):
    return _release(_local_module_closure(source))


def _release(modules):
    digest_builder = hashlib.sha256()
    total_bytes = 0
    for relative, data in sorted(modules.items(), key=lambda item: item[0].as_posix()):
        path_bytes = relative.as_posix().encode('utf-8')
        digest_builder.update(len(path_bytes).to_bytes(4, 'big'))
        digest_builder.update(path_bytes)
        digest_builder.update(len(data).to_bytes(8, 'big'))
        digest_builder.update(data)
        total_bytes += len(data)
    digest = digest_builder.hexdigest()
    relative = f'assistant/releases/{digest[:16]}/setup-chat.js'
    return {'sha256': digest, 'bytes': total_bytes, 'relative_path': relative,
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


def _ensure_public_root(public_dir):
    """Require a real public root and give only that root public traversal."""
    if public_dir.is_symlink():
        raise ValueError('assistant public root must not be a symlink')
    if public_dir.exists():
        if not public_dir.is_dir():
            raise ValueError('assistant public root must be a directory')
    else:
        public_dir.mkdir(parents=True, mode=0o755)
    public_dir.chmod(0o755)


def _ensure_public_directory(public_dir, directory):
    """Create and normalize only directories owned by the assistant bundle."""
    relative = directory.relative_to(public_dir)
    current = public_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('assistant public directory must not be a symlink')
        current.mkdir(mode=0o755, exist_ok=True)
        current.chmod(0o755)


def publish_assistant(public_dir, source):
    public_dir = Path(public_dir)
    modules = _local_module_closure(source)
    release = _release(modules)
    _ensure_public_root(public_dir)
    release_dir = (public_dir / release['relative_path']).parent
    for relative, data in modules.items():
        target = release_dir / relative
        _ensure_public_directory(public_dir, target.parent)
        if target.is_symlink():
            raise ValueError('assistant public module must not be a symlink')
        if target.exists():
            if not target.is_file():
                raise ValueError('assistant public module must be a regular file')
            if target.read_bytes() != data:
                raise ValueError('immutable assistant module differs')
        else:
            target.write_bytes(data)
        target.chmod(0o644)
    # Cached HTML can still import this address; it forwards to the same UI.
    shim = public_dir / 'integrations/setup-chat.js'
    _ensure_public_directory(public_dir, shim.parent)
    temporary = shim.with_suffix('.js.next')
    temporary.write_text('// Generated compatibility entry; source: distribution/src/habab_home/frontend/setup-chat.js.\n'
                         + f'import "{release["module_url"]}";\n', encoding='utf-8')
    temporary.chmod(0o644)
    os.replace(temporary, shim)
    return release
