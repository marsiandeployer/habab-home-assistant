"""The Android runtime manifest uses guest-root paths for Codex file hashes."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import package_runtime


class _InstallerLoader:
    def __init__(self, result):
        self.result = result

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.install = lambda *args, **kwargs: self.result


class _InstallerSpec:
    def __init__(self, result):
        self.loader = _InstallerLoader(result)


class RuntimeCodexInventoryTests(unittest.TestCase):
    def test_installer_leaf_names_are_mapped_to_manifest_guest_paths(self):
        digests = {
            'codex': 'a' * 64,
            'codex-code-mode-host': 'b' * 64,
            'codex-exec-server': 'c' * 64,
        }
        result = {'version': '0.166.0', 'files': digests}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(package_runtime.importlib.util, 'spec_from_file_location',
                             return_value=_InstallerSpec(result)), \
                patch.object(package_runtime.importlib.util, 'module_from_spec',
                             return_value=SimpleNamespace()):
            version, files = package_runtime.install_codex(Path(directory), Path(directory) / 'cache')

        self.assertEqual(version, '0.166.0')
        self.assertEqual(files, {
            'opt/habab-codex/codex': 'a' * 64,
            'opt/habab-codex/codex-code-mode-host': 'b' * 64,
            'opt/habab-codex/codex-exec-server': 'c' * 64,
        })
        release = {'codexVersion': version, 'codexFiles': files}
        release['codexSha256'] = release['codexFiles']['opt/habab-codex/codex']
        self.assertEqual(release['codexSha256'], 'a' * 64)

    def test_unexpected_installer_inventory_fails_before_manifest_creation(self):
        result = {'version': '0.166.0', 'files': {'codex': 'a' * 64}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(package_runtime.importlib.util, 'spec_from_file_location',
                             return_value=_InstallerSpec(result)), \
                patch.object(package_runtime.importlib.util, 'module_from_spec',
                             return_value=SimpleNamespace()):
            with self.assertRaisesRegex(ValueError, 'unexpected runtime file inventory'):
                package_runtime.install_codex(Path(directory), Path(directory) / 'cache')


if __name__ == '__main__':
    unittest.main()
