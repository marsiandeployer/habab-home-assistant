const releasePath = process.env.HABAB_TEXT_RELAY_RELEASE ||
  '/root/.local/share/habab-text-relay/current';
const privateConfig = process.env.HABAB_TEXT_RELAY_CONFIG ||
  '/root/.local/share/habab-text-relay/config.json';

module.exports = {apps: [{
  name: 'habab-text-relay',
  cwd: releasePath,
  script: 'openai_gateway.py',
  interpreter: process.env.HABAB_RELAY_PYTHON || '/root/.venvs/habab-voice-relay/bin/python',
  args: ['--config', privateConfig],
  autorestart: true,
  max_restarts: 5,
  restart_delay: 3000,
  kill_timeout: 5000,
  time: true
}]};
