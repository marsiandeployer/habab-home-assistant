"""Contract checks for the Windows Xiaomi callback preparation scripts.

PowerShell is not installed in the Linux CI environment, so these checks guard
the launcher/helper control-flow and preservation rules without pretending to
exercise Windows UAC or the real hosts file.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "distribution"


class WindowsCallbackContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = (ROOT / "start-windows.ps1").read_text(encoding="utf-8")
        cls.helper = (ROOT / "ensure-xiaomi-callback.ps1").read_text(encoding="utf-8")

    def test_ordinary_launcher_prepares_callback_only_on_registered_port(self):
        port_check = self.launcher.index("if ($portNumber -eq 8123)")
        helper_call = self.launcher.index("-File $callbackHelper", port_check)
        custom_port_message = self.launcher.index("registered callback uses port 8123")
        self.assertLess(port_check, helper_call)
        self.assertLess(helper_call, custom_port_message)
        self.assertIn("if ($callbackExitCode -eq 0)", self.launcher)

    def test_callback_failure_keeps_general_setup_available(self):
        failure_branch = self.launcher.index("} elseif ($XiaomiHome) {")
        general_fallback = self.launcher.index("Home Assistant will still start", failure_branch)
        next_setup_step = self.launcher.index("Building Home Assistant", general_fallback)
        self.assertLess(failure_branch, general_fallback)
        self.assertLess(general_fallback, next_setup_step)
        self.assertNotIn("exit 1", self.launcher[general_fallback:next_setup_step])

    def test_xiaomi_opt_in_still_requires_registered_port_and_explicit_installer(self):
        self.assertIn("if ($XiaomiHome -and $portNumber -ne 8123)", self.launcher)
        self.assertIn("if ($XiaomiHome) {", self.launcher)
        self.assertIn("habab-install-xiaomi-home", self.launcher)
        self.assertIn("if ($XiaomiHome) { $startArguments += '--force-recreate' }", self.launcher)

    def test_hosts_helper_is_additive_and_refuses_conflicts(self):
        self.assertIn("CONFLICT: homeassistant.local already has a different hosts-file mapping", self.helper)
        self.assertIn("[IO.File]::AppendAllText($hostsPath, $entry, $encoding)", self.helper)
        self.assertNotIn("[IO.File]::WriteAllText($hostsPath", self.helper)
        self.assertNotIn("Remove-Content", self.helper)
        self.assertNotIn("Set-Content", self.helper)

    def test_uac_decline_has_an_explicit_non_success_result(self):
        self.assertIn("UAC permission was declined. No hosts-file changes were made.", self.helper)
        self.assertIn("exit 3", self.helper)
        self.assertIn("exit 2", self.helper)


if __name__ == "__main__":
    unittest.main()
