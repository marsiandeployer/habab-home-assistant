"""Static contract checks for the scoped demo relay boot unit."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent
UNIT = ROOT / 'systemd/habab-demo-realtime-relay.service'
TARGET = 'habab-demo-realtime-relay'
PRIVATE_CONFIG = '/root/.local/share/habab-demo-realtime-relay/pm2/realtime-d411.config.cjs'
PM2 = '/root/.nvm/versions/node/v22.23.4/lib/node_modules/pm2/bin/pm2'


class DemoRealtimeSystemdUnitTests(unittest.TestCase):
    def test_boot_uses_the_immutable_private_d411_config(self):
        unit = UNIT.read_text()
        self.assertIn(f'{PM2} start {PRIVATE_CONFIG} --only {TARGET}', unit)
        self.assertNotIn('/root/homeassistent/habab/android/relay/ecosystem.demo-realtime.config.cjs', unit)
        self.assertNotIn('2bd37656ab1af10584abd5eb20231dac29b2ab21', unit)

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
