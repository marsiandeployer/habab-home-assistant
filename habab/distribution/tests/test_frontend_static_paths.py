"""Check hashed entry and its relative imports against registered static paths."""

import hashlib
from pathlib import Path
import re
import unittest
from urllib.parse import urljoin, urlsplit

from homeassistant.components import habab_home


def _resolve_static_path(request_path, static_paths):
    for config in static_paths:
        route = config.url_path.rstrip("/")
        if request_path == config.url_path:
            return Path(config.path)
        if request_path.startswith(route + "/"):
            relative_path = request_path[len(route) + 1:]
            return Path(config.path) / relative_path
    return None


class FrontendStaticPathsTests(unittest.TestCase):
    def test_hashed_chat_entry_relative_imports_resolve_under_registered_static_prefix(self):
        frontend_dir = Path(habab_home.__file__).parent / "frontend"
        assistant_file = frontend_dir / "setup-chat.js"
        digest = hashlib.sha256(assistant_file.read_bytes()).hexdigest()[:16]
        assistant_url, static_paths = habab_home._assistant_static_paths(frontend_dir, digest)

        assert assistant_url == f"/habab-home/{digest}.js"
        assert static_paths[0].url_path == assistant_url
        assert Path(static_paths[0].path) == assistant_file
        assert static_paths[1].url_path == "/habab-home"
        assert Path(static_paths[1].path) == frontend_dir

        source = assistant_file.read_text(encoding="utf-8")
        imports = re.findall(r'\bfrom\s+["\']([^"\']+)["\']', source)
        assert imports, "Expected the entry module to have static imports"

        entry_url = f"https://ha.invalid{assistant_url}"
        for specifier in imports:
            resolved_url = urljoin(entry_url, specifier)
            resolved_path = urlsplit(resolved_url).path
            local_file = _resolve_static_path(resolved_path, static_paths)
            assert local_file is not None, f"No registered static route for {resolved_path}"
            assert local_file.is_file(), f"Missing module served at {resolved_path}"
