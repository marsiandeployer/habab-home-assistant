# Public demo Realtime relay rollout

The isolated public demo relay runs the same gateway application as the Gemini
relay, on loopback port `18329` under a separate PM2 entry. The Gemini relay on
`8326`, demo backend, Nginx, and frontend are independent and must remain
untouched during a relay-only update.

## Current and candidate versions

The read-only preflight on 2026-10-27 found PM2 entry
`habab-demo-realtime-relay` online at PID `1825602` (PM2 id `68`), running
`/root/.local/share/habab-voice-relay/releases/d411e21e146106b8d1f8dd6074100ac18d3ff3b0/openai_gateway.py`.
The enabled systemd unit is currently inactive and its installed copy still
references the private `realtime-d411.config.cjs`.

The reviewed candidate source is commit
`2ca4e03167a87b98d66f24e34df233285c825923`. Its immutable gateway release is
`/root/.local/share/habab-voice-relay/releases/2ca4e03167a87b98d66f24e34df233285c825923/openai_gateway.py`.
The candidate keeps port `18329`, the exact configured demo Origin, the same
private control socket, and limits of five paid starts and two starts per
anonymous session in a rolling 24-hour window. The config hash remains private;
the upstream OpenAI key is still provided at bootstrap and held only in RAM.

Staged candidate config paths are:

- `/root/.local/share/habab-demo-realtime-relay/config-2ca4e031.json`
- `/root/.local/share/habab-demo-realtime-relay/pm2/realtime-2ca4e031.config.cjs`

The source systemd unit points to that PM2 config. These candidate files do not
replace the currently installed unit or restart the live relay.

## Validation and rollout boundary

The source commit is pinned by the PM2 candidate config and its gateway file
hash is checked against the committed Git blob. The private JSON keeps the
current port, origin and service-key hash while explicitly adding the
five/2/86400 rolling quotas. File and directory modes remain `0600` and
`0700`; the candidate process uses the same venv and control socket path.

Before any live action, re-read the PM2 PID, source path and unit files. A
scoped delete/start of only `habab-demo-realtime-relay` will reset its in-memory
global and per-session rolling history once. The existing usage count is not
exported by the process and is not read or changed by staging. Reserve at most
one live model session for a later authorized check; this staging step makes no
model request. Do not run `pm2 save`, since the dedicated systemd unit owns boot
startup and PM2's saved process list includes unrelated applications.

The loopback synthetic fixture is
`habab/qa/issue1-realtime-demo-20261026/dedicated-relay-contract.py`. It uses
the real Flask bootstrap route and gateway with a temporary Unix socket and a
loopback-only fake upstream. It sends no audio and makes no OpenAI request.

No live process, systemd unit, backend, Nginx config, or frontend is changed as
part of staging and synthetic validation.
