"""Static contract checks for the scoped demo relay boot unit."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent
UNIT = ROOT / 'systemd/habab-demo-realtime-relay.service'
TARGET = 'habab-demo-realtime-relay'
PRIVATE_CONFIG = '/root/.local/share/habab-demo-realtime-relay/pm2/realtime-2ca4e031.config.cjs'
RELEASE = '/root/.local/share/habab-voice-relay/releases/2ca4e03167a87b98d66f24e34df233285c825923'
PM2 = '/root/.nvm/versions/node/v22.23.4/lib/node_modules/pm2/bin/pm2'


class DemoRealtimeSystemdUnitTests(unittest.TestCase):
    def test_boot_uses_the_immutable_private_2ca4_config(self):
        unit = UNIT.read_text()
        self.assertIn(f'{PM2} start {PRIVATE_CONFIG} --only {TARGET}', unit)
        self.assertNotIn('/root/homeassistent/habab/android/relay/ecosystem.demo-realtime.config.cjs', unit)
        self.assertNotIn('2bd37656ab1af10584abd5eb20231dac29b2ab21', unit)
        self.assertNotIn('d411e21e146106b8d1f8dd6074100ac18d3ff3b0', unit)

    def test_pm2_candidate_pins_the_reviewed_gateway_and_windowed_config(self):
        candidate = (ROOT / 'ecosystem.demo-realtime.config.cjs').read_text()
        self.assertIn(f"cwd: '{RELEASE}'", candidate)
        self.assertIn(f"script: '{RELEASE}/openai_gateway.py'", candidate)
        self.assertIn("--config /root/.local/share/habab-demo-realtime-relay/config-2ca4e031.json", candidate)

    def test_boot_unit_is_ordered_after_pm2_and_scoped_to_one_process(self):
        unit = UNIT.read_text()
        self.assertIn('Requires=pm2-root.service', unit)
        self.assertIn('After=pm2-root.service', unit)
        self.assertIn('Type=oneshot', unit)
        self.assertIn('Environment=PM2_HOME=/root/.pm2', unit)
        self.assertIn(f"ExecCondition=/bin/sh -c '{PM2} ping >/dev/null 2>&1'", unit)
        self.assertIn('ExecCondition=/bin/sh -c', unit)
        self.assertIn(f'{PM2} pid {TARGET}', unit)
        self.assertIn('grep -E -q "^[1-9][0-9]*"', unit)
        self.assertIn(f'{PM2} start {PRIVATE_CONFIG} --only {TARGET}', unit)
        self.assertNotRegex(unit, r'(?m)^Exec(?:Start|Stop|Reload)=.*\b(?:all|save|resurrect|kill)\b')
        self.assertNotRegex(unit, r'(?m)^EnvironmentFile=')
        self.assertNotRegex(unit, r'(?m)^ExecStop=')


if __name__ == '__main__':
    unittest.main()
