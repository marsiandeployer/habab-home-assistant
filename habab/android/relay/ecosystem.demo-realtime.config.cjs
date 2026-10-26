module.exports = {apps: [{
  name: 'habab-demo-realtime-relay',
  cwd: '/root/.local/share/habab-voice-relay/releases/2ca4e03167a87b98d66f24e34df233285c825923',
  script: '/root/.local/share/habab-voice-relay/releases/2ca4e03167a87b98d66f24e34df233285c825923/openai_gateway.py',
  interpreter: '/root/.venvs/habab-voice-relay/bin/python',
  args: '--config /root/.local/share/habab-demo-realtime-relay/config-2ca4e031.json',
  exec_mode: 'fork', instances: 1, kill_timeout: 5000,
  autorestart: true, watch: false,
  env: {PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', TZ: 'UTC'},
}]};
