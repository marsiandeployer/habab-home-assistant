# Ordinary web-chat service-call E2E runner

`service-call-e2e.cjs` is prepared for the explicitly authorized non-Android execution window. It is **not** an E2E result and must not be run with `--run-live` until root confirms the cloud window is free.

## Test contract

The runner fails closed unless all of these match:

- `--expected-image`, `--expected-source`, and `--expected-runtime` match a freshly collected `--runtime-identity` JSON file from read-only Docker/source inspection. This is operator-attested release identity; the authenticated status endpoint does not attest the image digest or source revision. The identity file must describe the running image/source, not workspace `HEAD` or a local candidate. It accepts `image` or `image_sha256` plus `source` and `runtime` fields.
- Authenticated `GET /api/habab_home/status` reports the explicitly supplied product and Home Assistant versions and `running` status. The auth file supplies the access token; the runner uses the allowlisted owner-test origin and validates `base_url` if one is present. It never prints or copies credentials.
- The browser account is an active HA administrator. The existing chat must have no unsent draft; the runner does not clear history, call `/new`, or replace a draft.

The disposable helper setup is a separate native HA test-apparatus step, not a chat action. The runner creates one `input_boolean` through HA's `input_boolean/create` WebSocket command, captures the returned helper ID and exact entity-registry ID in private trace, and only then sends the test prompts through the actual browser chat UI. It refuses to reuse a helper with the same unique test name or target an entity outside the expected QA prefix.

Before the chat turns, an observer calibration directly toggles this exact fixture on and off through native HA WebSocket service calls. Those apparatus calls are separately recorded and are never counted as chat proof. Each must produce one matching `call_service` event with the exact fixture target and authenticated-user context, plus the matching independent REST state readback. If the observer does not see either calibration call, the runner stops before the model prompts; silence is not treated as proof that a later unknown-target turn made no call.

The four bounded prompts request: turn on the created helper, turn it off, act on a nonexistent named device, and report the currently connected devices and their types. The browser first loads `/owner-test`, then `/home/overview`, each with `networkidle2`; it records the actual pathnames. For each prompt, a one-time wrapper observes the actual `chat.send` Promise. Completion requires the current submission to be accepted, that Promise to settle, busy to clear, no UI error, and an exact trailing user-prompt/assistant-reply pair. Optional backend diagnostics are bounded to five seconds; unavailable diagnostics are reported explicitly, while an observed active backend fails acceptance. It does not depend on message count, so it handles the backend's 60-message history cap and cannot accept a preexisting matching pair if submit was skipped. The calibrated `call_service` event subscription observes service domain/name/targets through the complete chat turn. A listener on the exact HA connection object records its identity and every `disconnected` event; each calibration and chat case fails closed unless that same connection remains connected with zero disconnects through independent state readback. Therefore a zero-event negative case is not accepted if observer continuity was lost. The inventory case reads only the native device-registry count immediately before and after the prompt, requires the count to remain the same, expects no service calls, and verifies the disposable helper remains off. Its raw answer stays in the private trace for manual review; an unchanged count alone does not establish that the answer correctly described the devices. Positive action cases require exactly one expected service call targeting only the newly created helper, matching authenticated-user context, and an independent REST state readback. The runner never calls `setup_actions.execute` or `habab-ha` as a substitute for chat.

After the turns, native HA cleanup deletes only the helper ID returned by this run via `input_boolean/delete` (Home Assistant's helper API uses a helper ID, not a config-entry ID). The runner verifies the exact helper is absent from `input_boolean/list` and that its entity has no state. A completed automated run with successful observer calibration, action/readback cases, no-call cases, unchanged device-registry count, and exact helper cleanup ends `PASS_PENDING_MANUAL_REVIEW`; a human must inspect the private inventory answer before changing that verdict to `PASS`. The count proves only that registry size did not change, not that the assistant's device summary was correct. If the browser disconnects after fixture creation, the private trace retains the exact helper ID for owner cleanup; the public report is failure, not acceptance.

## Data handling

- Pass authentication by the private `--auth-file` argument. The token stays in process memory and the temporary owned Chromium profile; the profile is closed/removed on exit.
- The raw test prompts, assistant replies, and observed service targets are written mode `0600` next to the supplied private auth file. Attempt 2 uses `service-call-e2e-20261024-attempt-2-private.json`; the original private FAIL and its dated attempt-1 copy are preserved. This file is not a release artifact; keep it outside the repository and do not copy it into the public report.
- `service-call-e2e-20261024-attempt-2.json` contains only pass/fail booleans, sanitized case summaries, and expected runtime identity. The original `service-call-e2e.json` FAIL and its dated attempt-1 copy remain historical evidence. No entity IDs, raw messages, access token, or target URL are written there.
- No screenshots are produced. If a later run adds screenshots, open and inspect them before delivery.

## Read-only preflight

This checks only release identity and authenticated status. It does not open a browser, create a helper, send a chat turn, or write a report:

```sh
node habab/qa/issue-monitor-20261024/service-call-e2e.cjs \
  --auth-file "$HABAB_QA_AUTH_FILE" \
  --runtime-identity "$HABAB_QA_RUNTIME_IDENTITY" \
  --expected-image "$HABAB_QA_EXPECTED_IMAGE" \
  --expected-source "$HABAB_QA_EXPECTED_SOURCE" \
  --expected-runtime "$HABAB_QA_EXPECTED_RUNTIME" \
  --expected-product-version "$HABAB_QA_EXPECTED_PRODUCT_VERSION" \
  --expected-ha-version 2026.10.2
```

The status endpoint does not disclose the image digest or source revision, so the identity JSON must come from a separate fresh read-only runtime inspection. Missing or mismatched identity/status returns a short failure code and no test begins. The first preflight exposed a missing owner-session cookie in Node's stateless `fetch`; the runner now bootstraps the session from `/owner-test`, retains cookies in memory for both status and later REST state readbacks, and follows only same-origin HTTPS redirects. No cross-origin redirect is followed and cookie values are never logged. The corrected read-only preflight passed for the live v15 runtime after its Docker image digest/tag were independently read back; this is not an E2E result and must be repeated with fresh identity immediately before any separately authorized live run.

## Authorized execution

Only after root explicitly grants the test window, append `--run-live --execution-window-id "$HABAB_QA_EXECUTION_WINDOW"` to the command above. This runs the four bounded chat turns and the exact native fixture cleanup. It appends to the existing canonical chat session and leaves the new test messages in its history; it never clears prior history. Do not use this runner on physical devices or production household entities.

## Local checks performed during preparation

Syntax and local correlation checks:

```sh
node --check habab/qa/issue-monitor-20261024/service-call-e2e.cjs
node habab/qa/issue-monitor-20261024/service-call-e2e-completion.test.cjs
```

The synthetic check confirms a completed prompt/reply pair is accepted at exactly 60 items and rejects a preexisting pair without a busy cycle, an unfinished turn, an unrelated prompt, and a missing reply. The corrected read-only status preflight passed against owner-test v15 (`0.3.4`, Core `2026.10.2`, source `a3c1d3f06`, image `47c06194…`). No browser, model prompt, helper creation, service call, or cloud mutation has been performed as part of preparation.

## Live execution record

Attempt 1 on source `9d795359c22cf2844884caa5affc94209a8120ca` / image `e08f1857…` failed its original UI completion observation after ON passed. Exact fixture cleanup passed. Later read-only runtime and UI-reload inspection confirmed OFF execution, state `off`, and the persisted final reply; this does not change the original verdict. Attempt 2 is separately recorded with the corrected completion observation and a pre-cleanup failure snapshot. Consult its terminal report before claiming acceptance.

Attempt 2 ended `FAIL`: ON and OFF were accepted with exact service/context and independent readback, but the unknown-target turn lost its per-turn probe between waiting and snapshot. The final snapshot had an idle backend, exact reply tail, zero observed service events, and state `off`; completion correlation remains unproven. Exact helper deletion and owned-browser cleanup passed. No third live attempt was started.

The attempt-2 source-only candidate retained the completed Promise result before reading the current chat element and rejected fixture deletion unless backend idle was confirmed. Its synthetic checks passed, but its live result was FAIL as recorded above. The attempt-3 candidate adds connection-continuity checks and the verified native route sequence; it has not been run live. A fourth device-inventory case is now prepared: native read-only registry counts must match before and after the actual chat turn, while the human reviewer checks whether the private answer is factually accurate. Its source remains subject to root review and a separate exclusive-window authorization. Attempt 1 and attempt 2 outputs are preserved, and the runner refuses live execution if the selected attempt's public or private output path already exists.

Attempt 3 ended `FAIL_CLEANUP`: ON/OFF had exact service/context/readback and uninterrupted observation; the unknown-target send Promise completed but backend diagnostics became unavailable before full evidence capture. The device-inventory case was not reached. The automatic cleanup correctly retained the helper. Root separately confirmed the actual owner, backend idle, exact helper/entity binding, then deleted that helper and verified helper/state/registry absence. The original failure remains unchanged; `service-call-attempt3-operator-cleanup.json` records this operator cleanup separately. Do not run another attempt on the unchanged runtime merely to seek a pass.
