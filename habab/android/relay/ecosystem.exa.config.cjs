// Independent process: deploying search must not restart the live voice relay.
module.exports = { apps: [{
  name: 'habab-exa-mcp-relay',
  cwd: __dirname,
  script: 'exa_mcp_proxy.py',
  interpreter: process.env.HABAB_RELAY_PYTHON || '/root/.venvs/habab-voice-relay/bin/python',
  args: ['--config', process.env.HABAB_RELAY_CONFIG || '/root/.local/share/habab-voice-relay/config.json'],
  autorestart: true,
  max_restarts: 5,
  restart_delay: 3000,
  kill_timeout: 5000,
  time: true
}] };
