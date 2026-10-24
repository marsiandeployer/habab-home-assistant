# Provider status owner-test acceptance

This runner is prepared for the coherent Habab Home 0.3.5 release. It has not
been run against the cloud deployment. Run it only after the shopping baseline
is complete and the parent confirms the 0.3.5 deployment and QA window.

The runner uses the canonical owner route first so the owner-test routing cookie
is set, then opens `/habab-ai` on the same origin. It connects to the existing
headless CDP daemon on `127.0.0.1:9222` and creates isolated authenticated and
anonymous browser contexts. It does not use the user's Chrome profile.

The authenticated pass checks persisted provider/model selection, admin-only
saved-key readback, status labels from the server's provider list, pipeline-list
failure isolation, and status readback after a reload. The key is compared only
in browser memory. The report stores booleans and provider/model identifiers,
never the key or its fingerprint. Before taking a screenshot, the runner clears
the key field and sets its input type to password. Open every generated screenshot
and inspect it before sharing or publishing the evidence.

The `--check-selected` option performs one explicit `habab_home/providers/check`
request for the already selected, already saved provider. This may incur an API
charge and updates only its check metadata. It does not save a key, change the
selected provider, or send a chat prompt. The option is guarded by the release
and operator environment flags shown below. No invalid key is submitted and no
provider entry is altered to manufacture a rejected state. If the live entries
contain no rejected outcome, the report records that it was not observed; the
offline status tests cover rejection handling.

## Run after parent release signal

First confirm the deployed container image/source readback is the coherent 0.3.5
release. Confirm the shopping baseline is finished, the existing CDP daemon is
available on port 9222, and the parent has authorized the explicit selected-key
check. Then run:

```sh
python3 /root/.local/share/habab-owner-test/refresh-owner-auth.py refresh && \
HABAB_PROVIDER_STATUS_RELEASE_READY=0.3.5 \
HABAB_PROVIDER_STATUS_ALLOW_CHECK=yes \
node habab/qa/provider-status-20261024/owner-test.cjs --check-selected
```

The auth refresh writes the private `qa-current-auth.json` file with mode 0600.
The runner reads it into memory and never prints, writes, or copies its contents.
It preserves the shared Chrome process and closes only the contexts and pages it
created. Evidence is written under `habab/qa/provider-status-20261024/evidence/`.

## Acceptance evidence

- Exactly one persisted assistant provider is reported, with its persisted model.
- The summary remains the same after selecting a different editor tab.
- The saved key field is plain text for the authenticated administrator and
  matches the admin-only saved-key readback; only boolean results are recorded.
- The non-authenticated owner-test context cannot see the AI key input.
- Each live entry's UI status matches its server readback. A stale local terminal
  `verified` value cannot override a server `unknown` status.
- One authorized check of the selected saved entry returns `verified`; its
  timestamp and status remain identical after a full page reload.
- A rejected state is checked only if already present in a persisted native
  entry. Otherwise it is reported as not observed and no live credential is
  changed to create that case.
- A forced pipeline-list error is isolated to pipeline status; the provider list,
  selected provider/model, and API status remain readable. The browser stub is
  restored before cleanup.
- Screenshots show the status UI with the key field empty and masked. Inspect
  each screenshot before it is used as evidence.

No Windows tunnel or P30 validation belongs to this acceptance run.
