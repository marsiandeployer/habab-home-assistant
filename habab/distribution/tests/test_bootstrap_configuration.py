"""First boot enables native authoring without replacing any user config."""
from pathlib import Path
import subprocess
import tempfile
import unittest


DISTRIBUTION = Path(__file__).resolve().parents[1]


class BootstrapConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="habab-bootstrap-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = self.directory / "config"
        self.config.mkdir()
        # Change filesystem roots only. Execute the actual shell branch and
        # commands, ending with /bin/true instead of starting Home Assistant.
        script = (DISTRIBUTION / "docker-entrypoint.sh").read_text()
        script = script.replace("/config/", str(self.config) + "/")
        script = script.replace("mkdir -p /config\n", "mkdir -p " + str(self.config) + "\n")
        script = script.replace("/usr/local/share/habab-home/configuration.yaml", str(DISTRIBUTION / "config/configuration.yaml"))
        self.entrypoint = self.directory / "entrypoint.sh"
        self.entrypoint.write_text(script)

    def run_bootstrap(self):
        subprocess.run(["sh", str(self.entrypoint), "/bin/true"], check=True, capture_output=True)

    def test_fresh_first_boot_creates_native_include_and_empty_automation_list(self):
        self.run_bootstrap()
        actual = (self.config / "configuration.yaml").read_bytes()
        self.assertEqual(actual, (DISTRIBUTION / "config/configuration.yaml").read_bytes())
        self.assertIn(b"input_boolean:\n", actual)
        self.assertIn(b"automation: !include automations.yaml\n", actual)
        self.assertEqual((self.config / "automations.yaml").read_bytes(), b"[]\n")

    def test_existing_custom_config_and_own_automation_location_are_untouched(self):
        original = b"automation: !include user_rules.yaml\ninput_boolean:\n"
        rules = b"- id: keep_original\n  alias: Existing user rule\n"
        (self.config / "configuration.yaml").write_bytes(original)
        (self.config / "user_rules.yaml").write_bytes(rules)
        self.run_bootstrap()
        self.assertEqual((self.config / "configuration.yaml").read_bytes(), original)
        self.assertEqual((self.config / "user_rules.yaml").read_bytes(), rules)
        self.assertFalse((self.config / "automations.yaml").exists())

    def test_first_boot_keeps_preexisting_automations_file(self):
        original = b"- id: retained_rule\n  alias: Keep this file\n"
        (self.config / "automations.yaml").write_bytes(original)
        self.run_bootstrap()
        self.assertEqual((self.config / "automations.yaml").read_bytes(), original)
        self.assertTrue((self.config / "configuration.yaml").is_file())

    def test_first_boot_does_not_follow_or_replace_an_existing_dangling_symlink(self):
        missing = self.config / "user_configuration.yaml"
        link = self.config / "configuration.yaml"
        link.symlink_to(missing.name)
        self.run_bootstrap()
        self.assertTrue(link.is_symlink())
        self.assertFalse(missing.exists())
        self.assertFalse((self.config / "automations.yaml").exists())

    def test_first_boot_keeps_automation_symlink_and_its_absent_target(self):
        target = self.config / "user_rules.yaml"
        link = self.config / "automations.yaml"
        link.symlink_to(target.name)
        self.run_bootstrap()
        self.assertTrue(link.is_symlink())
        self.assertFalse(target.exists())
        self.assertTrue((self.config / "configuration.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
