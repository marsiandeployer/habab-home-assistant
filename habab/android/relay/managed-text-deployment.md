# Managed text relay process split

This is an uninstalled source configuration for separating the managed text
API and beta enrollment from the existing Gemini/Realtime relay. Nginx sends
`/beta/enroll`, `/text/capabilities`, `/v1/capabilities`,
`/v1/realtime/bootstrap`, `/v1/models`, and `/v1/chat/completions` to loopback
`127.0.0.1:18328`. The text relay verifies the incoming owner or hb1 credential.
When its optional private `voice_service_key` is configured, it calls only the
fixed voice endpoints on `127.0.0.1:8326`, using that server-held credential.
The field is optional for a text-only process, but required before routing
`/v1/capabilities` and `/v1/realtime/bootstrap` through it. Without that
credential, keep those two Nginx routes on the existing voice process.
The voice relay still owns Gemini token creation and `/gemini/ws`; Nginx keeps
the WebSocket and other voice routes on `127.0.0.1:8326`. Exa locations remain
on `127.0.0.1:8327`.
Bootstrap accepts up to 512 KiB of JSON wire data to account for escaped
Unicode; the normalized setup itself remains limited to 64 KiB.

The managed `habab-chat` alias fixes DeepSeek requests to non-thinking mode,
including ordinary chat, automatic or forced tools, and tool continuations.
It overrides `thinking` to `disabled` and any supplied `reasoning_effort` to
`none`, so the short, fast alias does not depend on forwarding
`reasoning_content`. Upstream errors are returned as errors; the relay does not
retry in thinking mode or fall back to a direct provider. Explicit OpenAI,
DeepSeek, and Google Gemini connections are unchanged.

`ecosystem.managed-text.config.cjs` starts a separate instance of the existing
frozen `openai_gateway.py` implementation with its existing `--config <path>`
interface. The config may include the optional `voice_service_key` field for
the fixed voice bridge. The private config must set port `18328` and retain the
existing config schema. The optional
`HABAB_TEXT_RELAY_RELEASE` and `HABAB_TEXT_RELAY_CONFIG` environment overrides
are filesystem paths only; `HABAB_RELAY_PYTHON` selects the interpreter.
Do not put service keys or upstream keys in this PM2 file, its environment, or
command arguments.

The process needs its own private config directory and `control.sock`. The
directory must be owned by the service user with mode `0700`; the JSON config
must be a regular file owned by that user with mode `0600`. If configured,
`voice_service_key` must be the separately approved legacy client-service
credential accepted by the existing voice relay. It is read only from that
private config and is never forwarded to clients or logged.

Enroll the managed DeepSeek upstream key for `purpose=text` through that
process's control socket so it remains in RAM. Do not share the voice process's
config, socket, or Google upstream key. Restarting the managed text process
clears its DeepSeek key from RAM; load it again through that process's own
control socket after any restart. The current service-key hash/config are
private state and must be provisioned with the existing owner workflow.

Disabling `issue_codes_enabled` makes `/beta/enroll` return the existing 403
response for new enrollments. It does not revoke hb1 codes already issued. The
Nginx enrollment location applies a per-client request limit in addition to
the relay's in-process limits.

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
  Verify enrollment, capabilities, bootstrap, public WSS, and the three text
  paths separately before beta.
