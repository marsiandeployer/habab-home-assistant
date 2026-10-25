import os
import stat
import tempfile
import unittest
from pathlib import Path

from assistant_bundle import LEGACY_TAG, OLD_START, OLD_END, assistant_release, publish_assistant, render_entrypoint


class SharedAssistantTests(unittest.TestCase):
    def test_legacy_and_preview_documents_migrate_to_the_same_single_module(self):
        prefix = '<html data-habab-demo="true"><script src="compiler.js"></script>'
        suffix = '</html>'
        legacy = prefix + LEGACY_TAG + suffix
        preview = prefix + OLD_START + '<script type="module">import(preview?"old-a":"old-b")</script>' + OLD_END + suffix
        one = render_entrypoint(legacy, '/one-shared-module.js')
        self.assertEqual(render_entrypoint(preview, '/one-shared-module.js'), one)
        self.assertEqual(render_entrypoint(one, '/one-shared-module.js'), one)
        self.assertTrue(one.startswith(prefix))
        self.assertTrue(one.endswith(suffix))
        self.assertEqual(one.count('/one-shared-module.js'), 1)
        self.assertNotIn('old-a', one)

    def test_multiple_entries_are_rejected(self):
        for html in (LEGACY_TAG * 2, LEGACY_TAG + OLD_START + OLD_END, OLD_START, ''):
            with self.assertRaises(ValueError):
                render_entrypoint(html, '/one-shared-module.js')

    def test_compatibility_entry_forwards_to_the_same_immutable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'setup-chat.js'
            source.write_text('export const oneComponent = true;\n')
            public = base / 'public'
            release = publish_assistant(public, source)
            artifact = public / release['relative_path']
            self.assertEqual(artifact.read_bytes(), source.read_bytes())
            shim = public / 'integrations/setup-chat.js'
            self.assertIn('import "' + release['module_url'] + '";', shim.read_text())
            self.assertNotIn('oneComponent', shim.read_text())
            self.assertEqual(publish_assistant(public, source), release)
            self.assertEqual(len(list((public / 'assistant/releases').glob('*/setup-chat.js'))), 1)
            artifact.write_text('unexpected content')
            with self.assertRaises(ValueError):
                publish_assistant(public, source)

    def test_import_closure_is_published_and_changes_release_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source_dir = base / 'frontend'
            nested = source_dir / 'nested'
            nested.mkdir(parents=True)
            source = source_dir / 'setup-chat.js'
            strings = source_dir / 'strings.js'
            voice = nested / 'voice.js'
            shared = source_dir / 'shared.js'
            source.write_text('import "./strings.js";\nimport("./nested/voice.js");\n')
            strings.write_text('export const strings = { label: "first" };\n')
            voice.write_text('import { shared } from "../shared.js";\n')
            shared.write_text('export const shared = true;\n')

            public = base / 'public'
            first_release = publish_assistant(public, source)
            first_dir = (public / first_release['relative_path']).parent
            for relative in ('setup-chat.js', 'strings.js', 'nested/voice.js', 'shared.js'):
                self.assertEqual((first_dir / relative).read_bytes(), (source_dir / relative).read_bytes())
            self.assertEqual(assistant_release(source), first_release)

            strings.write_text('export const strings = { label: "changed" };\n')
            second_release = assistant_release(source)
            self.assertNotEqual(first_release['sha256'], second_release['sha256'])
            publish_assistant(public, source)
            second_dir = (public / second_release['relative_path']).parent
            for relative in ('setup-chat.js', 'strings.js', 'nested/voice.js', 'shared.js'):
                self.assertTrue((second_dir / relative).is_file(), relative)

            source.write_text('import "./missing.js";\n')
            with self.assertRaisesRegex(ValueError, 'missing assistant import'):
                assistant_release(source)

    def test_publish_modes_stay_public_under_restrictive_umask_without_chmodding_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source_dir = base / 'frontend'
            source_dir.mkdir()
            nested = source_dir / 'nested'
            nested.mkdir()
            source = source_dir / 'setup-chat.js'
            strings = source_dir / 'strings.js'
            voice = nested / 'voice.js'
            source.write_text('import "./strings.js";\nimport "./nested/voice.js";\n')
            strings.write_text('export const strings = {};\n')
            voice.write_text('export const voice = {};\n')
            for path in (source, strings, voice):
                path.chmod(0o600)

            public = base / 'public'
            private_sibling = base / 'private-sibling'
            private_sibling.mkdir()
            private_sibling.chmod(0o700)
            sibling_file = private_sibling / 'private.json'
            sibling_file.write_text('{}\n')
            sibling_file.chmod(0o600)
            previous_umask = os.umask(0o077)
            try:
                release = publish_assistant(public, source)
            finally:
                os.umask(previous_umask)

            release_dir = (public / release['relative_path']).parent
            for directory_path in (public, public / 'assistant', public / 'assistant/releases', release_dir,
                                   release_dir / 'nested',
                                   public / 'integrations'):
                self.assertEqual(stat.S_IMODE(directory_path.stat().st_mode), 0o755,
                    directory_path.relative_to(public).as_posix())
            for relative in ('setup-chat.js', 'strings.js', 'nested/voice.js'):
                output = release_dir / relative
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644, relative)
                self.assertEqual(output.read_bytes(), (source_dir / relative).read_bytes())
            private_module = release_dir / 'setup-chat.js'
            original_bytes = private_module.read_bytes()
            private_module.chmod(0o600)
            previous_umask = os.umask(0o077)
            try:
                repaired_release = publish_assistant(public, source)
            finally:
                os.umask(previous_umask)
            self.assertEqual(repaired_release['sha256'], release['sha256'])
            self.assertEqual(private_module.read_bytes(), original_bytes)
            self.assertEqual(stat.S_IMODE(private_module.stat().st_mode), 0o644)
            shim = public / 'integrations/setup-chat.js'
            self.assertEqual(stat.S_IMODE(shim.stat().st_mode), 0o644)
            for path in (source, strings, voice):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600,
                    path.relative_to(source_dir).as_posix())
            self.assertEqual(stat.S_IMODE(private_sibling.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(sibling_file.stat().st_mode), 0o600)

    def test_invalid_public_root_fails_before_writing_bundle_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'setup-chat.js'
            source.write_text('export const oneComponent = true;\n')

            symlink_target = base / 'target'
            symlink_target.mkdir()
            public_symlink = base / 'public-link'
            public_symlink.symlink_to(symlink_target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'public root must not be a symlink'):
                publish_assistant(public_symlink, source)
            self.assertEqual(list(symlink_target.iterdir()), [])

            public_file = base / 'public-file'
            public_file.write_bytes(b'keep')
            with self.assertRaisesRegex(ValueError, 'public root must be a directory'):
                publish_assistant(public_file, source)
            self.assertEqual(public_file.read_bytes(), b'keep')

    def test_existing_module_symlink_is_rejected_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'setup-chat.js'
            source.write_text('export const oneComponent = true;\n')
            public = base / 'public'
            public.mkdir()
            release = assistant_release(source)
            module = (public / release['relative_path'])
            module.parent.mkdir(parents=True)
            target = base / 'private-target.js'
            target.write_bytes(b'private')
            module.symlink_to(target)

            with self.assertRaisesRegex(ValueError, 'public module must not be a symlink'):
                publish_assistant(public, source)
            self.assertTrue(module.is_symlink())
            self.assertEqual(target.read_bytes(), b'private')
            self.assertFalse((public / 'integrations/setup-chat.js').exists())


if __name__ == '__main__':
    unittest.main()
