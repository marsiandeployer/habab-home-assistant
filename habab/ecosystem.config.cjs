module.exports = {apps: [{
  name: 'habab-home-demo',
  cwd: '/root/homeassistent/habab',
  script: '/root/homeassistent/.venv/bin/gunicorn',
  interpreter: 'none',
  args: '--workers 2 --threads 2 --worker-class gthread --bind 127.0.0.1:8318 --timeout 45 --access-logfile - --error-logfile - app:application',
  exec_mode: 'fork', instances: 1, kill_timeout: 35000,
  env: {
    NODE_ENV: 'production',
    HA_DEMO_REALTIME_CONTROL_SOCKET: '/root/.local/share/habab-demo-realtime-relay/control.sock',
  },
}]};
