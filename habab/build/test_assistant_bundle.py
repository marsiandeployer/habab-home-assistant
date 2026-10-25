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


if __name__ == '__main__':
    unittest.main()
