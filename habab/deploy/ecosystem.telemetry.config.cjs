module.exports = {apps: [{
  name: 'habab-telemetry-cleanup',
  cwd: '/root/homeassistent/habab',
  script: '/root/homeassistent/habab/support/telemetry-cleanup.py',
  interpreter: '/root/homeassistent/.venv/bin/python3',
  autorestart: false,
  cron_restart: '0 * * * *',
}]};
