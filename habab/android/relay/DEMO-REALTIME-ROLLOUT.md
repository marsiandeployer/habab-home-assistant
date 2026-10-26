# Public demo Realtime relay instance

This candidate runs the verified `openai_gateway.py` program as a separate
PM2 process on loopback port `18329`. It does not replace the Gemini relay on
`8326`; all existing Gemini paths continue through the current generic Nginx
location. The new process has its own config and Unix control socket at
`/root/.local/share/habab-demo-realtime-relay/control.sock`.

## Request path

1. The browser posts same-origin to `/api/ha-demo/realtime/bootstrap`.
2. `habab-home-demo` reads the upstream key from its existing private `.env`
   and sends it with a session hash through the dedicated Unix socket.
3. The demo relay returns a one-use grant. The browser sends that grant as the
   first WebSocket frame to `wss://habab-voice.wpmix.net/openai/demo/ws`.
4. The exact Nginx location in
   `habab/deploy/nginx-voice-demo-realtime-location.conf` proxies only that
   path to `127.0.0.1:18329`. Other voice paths keep using `8326`.

The demo process accepts only the exact demo Origin, limits concurrent voice
connections to two, allows five starts per process and two starts per
anonymous session, and expires each grant after one minute. The process-wide
start budget is in memory and resets when this demo process restarts; it is
not a daily quota. The dedicated process receives no Gemini key. Its OpenAI
key is provided per bootstrap and held only in memory.

## Candidate apply sequence

This document and its config files are review artifacts. No production
process, directory, socket, or Nginx file has been changed by preparing them.

1. Install the verified source artifact from commit
   `2bd37656ab1af10584abd5eb20231dac29b2ab21` into the immutable release
   directory named in `ecosystem.demo-realtime.config.cjs`. Keep the active
   `9c174aacc` release intact.
2. Run `python3 habab/android/relay/prepare_demo_realtime_config.py` once. It reads only the existing
   relay `key_sha256` and exact demo Origin, and creates the new private
   directory/config with modes `0700`/`0600`, port `18329`, and explicit
   quotas. It refuses to overwrite an existing destination.
3. Run `pm2 start habab/android/relay/ecosystem.demo-realtime.config.cjs`.
   This starts only `habab-demo-realtime-relay` using the same checked gateway
   code with the new config; it does not bind or unlink the Gemini relay's
   `8326` listener or control socket. Run `pm2 save` after confirming the new
   process is online.
4. Run `pm2 startOrRestart habab/ecosystem.config.cjs`. That file declares
   only `habab-home-demo`; its new environment value selects the unique demo
   relay socket. The route reads
   `HA_DEMO_REALTIME_CONTROL_SOCKET` from the process environment; changing
   the PM2 ecosystem does not update a running Gunicorn process. The required
   restart is scoped to the demo backend and leaves both voice relay
   processes untouched.
5. Add the location snippet to the active `habab-voice.wpmix.net` server and
   add `wss://habab-voice.wpmix.net` to the active demo CSP. Run `nginx -t`
   and reload Nginx. The existing voice vhost already forwards WebSocket
   upgrades to `8326`; the exact location sends only `/openai/demo/ws` to
   `18329`. After the location is active, a request with a deliberately
   disallowed Origin can confirm routing without creating an upstream session;
   the relay should return `403` before checking for an OpenAI key.
6. Publish the verified demo frontend artifact last, after backend, relay,
   and Nginx are ready. Do not use a real OpenAI request as a deployment
   smoke test. A later HTTP request with a deliberately disallowed Origin may
   confirm that the public route reaches the new relay; it should be rejected
   before any upstream connection.

## Boot persistence candidate

The prepared `systemd/habab-demo-realtime-relay.service` is scoped to this one
PM2 process and is not installed or enabled yet. It uses the owner-only,
immutable `/root/.local/share/habab-demo-realtime-relay/pm2/realtime-d411.config.cjs`
outside the checkout. For a later relay release, verify the new private config
path, ownership, permissions and digest, then update the unit and its static
test together. Do not use global `pm2 save` to persist this service.

## Cleanup and rollback

To disable the candidate, first remove the exact Nginx location and reload
after `nginx -t`, then delete only `habab-demo-realtime-relay` from PM2 and
run `pm2 save`. Remove only
`/root/.local/share/habab-demo-realtime-relay/` after confirming the process
has stopped. Do not stop `habab-voice-relay`, remove its `control.sock`, or
delete its active/previous releases. The source release directory remains
available for review and rollback.

The local synthetic contract fixture is
`habab/qa/issue1-realtime-demo-20261026/dedicated-relay-contract.py`. It uses
the actual Flask route, the candidate Gateway, a temporary Unix socket, and a
loopback-only fake upstream. It sends no audio and makes no model request.
