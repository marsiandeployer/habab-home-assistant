"""Factory reuse must preserve dependency pins and budget the actual local base."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('factory_runtime', Path(__file__).parents[1] / 'tools/package_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class FactoryReuseTests(unittest.TestCase):
    def compatible(self):
        lock = json.loads((runtime.ROOT / 'tools/network_tools/node-apks.lock.json').read_text())
        return {
            'architecture': 'aarch64', 'coreVersion': '2026.10.2', 'baseImage': runtime.IMAGE,
            'opencodeVersion': json.loads((runtime.DIST / 'opencode-release.json').read_text())['arm64']['version'],
            'nodePackages': {name: record['sha256'] for name, record in lock['packages'].items()
                             if not name.startswith('icu-data-en-')},
            'networkTools': {key: runtime.sha256(runtime.ROOT / 'tools/network_tools' / name)
                             for key, name in (('lockSha256', 'network-tools.lock.json'),
                                               ('browserLauncherSha256', 'habab-browser'),
                                               ('pythonLauncherSha256', 'habab-python-network'))},
        }

    def test_reuse_rejects_changed_core_opencode_node_or_network_tools(self):
        release = self.compatible()
        runtime.validate_factory_dependencies(release, True)
        for field, value in (('architecture', 'amd64'), ('baseImage', 'other'),
                             ('coreVersion', '2026.11.0'), ('opencodeVersion', 'other'),
                             ('nodePackages', {})):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    runtime.validate_factory_dependencies({**release, field: value}, True)
        with self.assertRaisesRegex(ValueError, 'selection'):
            runtime.validate_factory_dependencies({**release, 'networkTools': None}, True)
        changed = copy.deepcopy(release)
        changed['networkTools']['lockSha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'network-tools input'):
            runtime.validate_factory_dependencies(changed, True)

    def test_existing_factory_archive_does_not_need_the_cold_export_disk_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            factory = {'unpackedBytes': 3 * 1024 ** 3, 'bytes': 1 * 1024 ** 3}
            with patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=6 * 1024 ** 3)):
                runtime.require_build_space(cache, cache / 'rootfs', True, factory)
                with self.assertRaises(OSError):
                    runtime.require_build_space(cache, cache / 'rootfs', True)
            with patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=4 * 1024 ** 3)):
                with self.assertRaises(OSError):
                    runtime.require_build_space(cache, cache / 'rootfs', True, factory)

    def test_factory_packaging_never_calls_download_or_docker_producers(self):
        factory = {**self.compatible(), 'id': 'factory', 'sourceRevision': 'b' * 40,
                   'url': 'https://example.invalid/factory.tgz', 'sha256': '0' * 64,
                   'bytes': 1024, 'unpackedBytes': 1024, 'opencodeSha256': '1' * 64}
        original_run = subprocess.run
        original_open = Path.open
        lock_files = []
        def open_file(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path.name == 'package.lock':
                lock_files.append(handle)
            return handle
        candidate_helper = runtime.ROOT.parent / 'native-frontend/candidate.py'
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            android = work / 'habab/android'
            distribution = work / 'habab/distribution'
            (android / 'runtime').mkdir(parents=True)
            (android / 'runtime/launch.py').write_text('launcher')
            (distribution / 'config').mkdir(parents=True)
            (distribution / 'config/configuration.yaml').write_text('automation: !include automations.yaml\n')
            (distribution / 'src/habab_home').mkdir(parents=True)
            (distribution / 'src/habab_home/manifest.json').write_text('{"version":"test"}')
            helper = work / 'habab/native-frontend/candidate.py'
            helper.parent.mkdir()
            shutil.copyfile(candidate_helper, helper)
            candidate = work / 'candidate'
            candidate.mkdir()
            wheel_name = 'home_assistant_frontend-20260930.4+habab.14-py3-none-any.whl'
            (candidate / wheel_name).write_bytes(b'wheel')
            (candidate / 'manifest.json').write_text(json.dumps({
                'source_revision': 'a' * 40, 'source_clean_before_build': True, 'builder_checks': 'passed',
                'wheel_version': '20260930.4+habab.14', 'artifact': wheel_name,
                'artifact_bytes': 5, 'artifact_sha256': hashlib.sha256(b'wheel').hexdigest(),
            }))
            with tarfile.open(work / 'factory.tgz', 'w:gz') as archive:
                item = tarfile.TarInfo('etc')
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            commands = []
            def run(command, **kwargs):
                commands.append(command[0])
                if command[0] == 'tar':
                    return original_run(command, **kwargs)
                self.assertEqual(command[:2], ['openssl', 'pkey'])
                return SimpleNamespace(returncode=0)
            arguments = ['producer', '--signing-key', str(work / 'unused-test-key'),
                         '--public-base', 'https://example.invalid', '--source-revision', 'a' * 40,
                         '--frontend-candidate', str(candidate), '--base-runtime-manifest',
                         str(work / 'factory.json'), '--network-tools']
            with patch.object(runtime, 'ROOT', android), patch.object(runtime, 'DIST', distribution), \
                    patch.object(sys, 'argv', arguments), \
                    patch.object(runtime, 'validate_source_revision'), \
                    patch.object(runtime, 'reusable_factory_release', return_value=factory), \
                    patch.object(runtime, 'copy_sources', return_value={}), \
                    patch.object(runtime, 'install_serialx_unix', return_value={}), \
                    patch.object(runtime, 'install_frontend', return_value={'version': 'fresh'}), \
                    patch.object(runtime, 'export_image', side_effect=AssertionError('Docker export forbidden')), \
                    patch.object(runtime, 'install_opencode', side_effect=AssertionError('download forbidden')), \
                    patch.object(runtime, 'install_node', side_effect=AssertionError('Docker forbidden')), \
                    patch.object(runtime, 'install_network_tools', side_effect=AssertionError('Docker forbidden')), \
                    patch.object(Path, 'open', open_file), \
                    patch.object(runtime.subprocess, 'run', side_effect=run), \
                    patch.object(runtime.subprocess, 'check_output', return_value=b'test-signature'):
                try:
                    runtime.main()
                finally:
                    for lock in lock_files:
                        lock.close()
            self.assertEqual(commands, ['tar', 'openssl'])
            release = json.loads(next((android / 'dist').glob('release-*.json')).read_text())
            self.assertEqual(release['baseRuntime']['id'], 'factory')
            self.assertEqual(release['sourceRevision'], 'a' * 40)
            self.assertEqual(release['nodePackages'], factory['nodePackages'])
            self.assertEqual(release['networkTools'], factory['networkTools'])
            self.assertTrue((work / 'factory.tgz').exists(), 'Factory input must never be removed')
