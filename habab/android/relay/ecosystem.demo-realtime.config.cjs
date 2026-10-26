module.exports = {apps: [{
  name: 'habab-demo-realtime-relay',
  cwd: '/root/.local/share/habab-voice-relay/releases/2bd37656ab1af10584abd5eb20231dac29b2ab21',
  script: '/root/.local/share/habab-voice-relay/releases/2bd37656ab1af10584abd5eb20231dac29b2ab21/openai_gateway.py',
  interpreter: '/root/.venvs/habab-voice-relay/bin/python',
  args: '--config /root/.local/share/habab-demo-realtime-relay/config.json',
  exec_mode: 'fork', instances: 1, kill_timeout: 5000,
  autorestart: true, watch: false,
  env: {PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', TZ: 'UTC'},
}]};
