# Managed text relay process split

This is an uninstalled source configuration for separating the managed text
API from the existing Gemini/Realtime relay. The VM104 Nginx vhost sends only
`/text/capabilities`, `/v1/models`, and `/v1/chat/completions` to loopback
`127.0.0.1:18328`. Gemini/Realtime and other relay routes remain on
`127.0.0.1:8326`; the Exa locations remain on `127.0.0.1:8327`.

`ecosystem.managed-text.config.cjs` starts a separate instance of the existing
frozen `openai_gateway.py` implementation with its existing `--config <path>`
interface. It does not copy or modify the gateway source. That process also
has the gateway's other handlers on loopback; Nginx publishes only the three
managed text paths from this instance. The private config must set port
`18328` and retain the existing config schema. The optional
`HABAB_TEXT_RELAY_RELEASE` and `HABAB_TEXT_RELAY_CONFIG` environment overrides
are filesystem paths only; `HABAB_RELAY_PYTHON` selects the interpreter.
Do not put service keys or upstream keys in this PM2 file, its environment, or
command arguments.

The process needs its own private config directory and `control.sock`. Enroll
the managed DeepSeek upstream key for `purpose=text` through that process's
control socket so it remains in RAM. Do not share the voice process's config,
socket, or Google upstream key. The current service-key hash/config are private
state and must be provisioned with the existing owner workflow.

## Apply gates

- This source change does not install or start the process and does not change
  live Nginx or PM2 state.
- Port `18326` is currently reserved by the provider13 candidate execution;
  do not repurpose or stop it from this workflow. The managed-text process is
  assigned the separate loopback port `18328`.
- Before any live apply, require the frozen SDK proof and explicit parent GO;
  stage the exact approved gateway release and private config, validate the
  isolated Nginx config and PM2 config, then apply through the reviewed owner.
- Keep `habab-voice-relay` on `8326` running without restart or config change.
  Verify the new text process and the three text paths separately before beta.
