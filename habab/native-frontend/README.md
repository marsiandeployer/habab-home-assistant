# Matched Home Assistant frontend bridge

This patch targets Home Assistant frontend tag `20260930.4`, commit
`26690e06b0bf64a1487929245c32842ef92c62b5`. It adds the admin-only assistant
handoff and the versioned `window.hababNativeUi` bridge while retaining the
native dialog fallback.

Bridge patch SHA-256: `b4f82672cd8ba145fa0a1b0739a81d38983139fe61fe17fc7b9faaf0ea0db76f`.

## Rebuild

Use a fresh detached worktree, pinned Node and pnpm versions, and the matching
stock translation catalog from the public Home Assistant Core 2026.10.2 image
digest below. The catalog is extracted from the installed stock frontend wheel;
no GitHub credential is needed.

```sh
git fetch https://github.com/home-assistant/frontend.git \
  refs/tags/20260930.4:refs/tags/20260930.4
test "$(git rev-parse 'refs/tags/20260930.4^{commit}')" = \
  26690e06b0bf64a1487929245c32842ef92c62b5
git worktree add --detach \
  /root/.local/share/habab-native-frontend-build \
  26690e06b0bf64a1487929245c32842ef92c62b5

cid="$(docker create --entrypoint /bin/true \
  ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4)"
mkdir -p /root/.local/share/habab-native-frontend-translations-stock
docker cp "$cid:/usr/local/lib/python3.14/site-packages/hass_frontend/static/translations" \
  /root/.local/share/habab-native-frontend-translations-stock/
docker rm "$cid"

bash habab/native-frontend/build-wheel.sh
```

The script requires the exact pinned source commit, an unmodified worktree, a
matching translation directory with Russian catalogs, Node `24.21.0`, and pnpm
`11.28.0`. It runs the upstream production Gulp stages in order, inserts the
matching stock catalogs and hashes into generated translation metadata before
Rspack bundles it, verifies every declared locale and fragment resolves to a
stock file, then packages the assets as
`home_assistant_frontend-20260930.4+habab.3-py3-none-any.whl`.

The catalog check fingerprints all 19 `ru-*.json` files by relative path and
content SHA-256. Expected catalog fingerprint:
`154cc8ea8822c85d63b46e34ccc667cbac01df2cbf8cacb0a300bfe1fecda3ce`.
The artifact directory is keyed by the final wheel SHA-256, matching the
installer URL convention.

The frontend asset version remains `20260930.4`; `+habab.3` is Python wheel
metadata used to distinguish the bridge build while preserving Core version
compatibility. Output is stored under a directory named for the bridge patch
SHA-256 in `habab/public/downloads/native-frontend/`; the installer uses the
final wheel SHA-256 directory published by the build script.

## Bridge API

- `window.hababAssistant` is the widget-owned bridge. The core frontend calls
  `open(params, opener)` only for admin users and preserves the native dialog
  if the bridge is absent or returns anything other than `true`.
- The Lovelace toolbar Assist row is hidden only while an admin assistant is
  available. It listens for `habab-assistant-availability` and removes the
  listener when the Lovelace root disconnects.
- `window.hababNativeUi` is version 1. `await loadForm()` resolves with the
  native `ha-form` module after `ha-form` and `ha-selector` are registered.
- Generic config and options flows use `loadFlowForm({ hass, flowType,
  handler, domain, step, submit, entryId? })`. `entryId` is required for an
  options flow. It returns the registered `step-flow-form`
  tag, the `habab-native-form-context` tag, and a flow config that reuses Home
  Assistant’s native translation and field callbacks. Render the native step
  form inside the context host with `.narrow=${true}` and call its public
  `.submit()`. It emits the
  native `flow-update` event with the next step. The context host uses HA’s
  `contextMixin`, including lazy providers, so selectors outside the app shell
  receive the same contexts as built-in flows.

## Checks

Focused source tests:

```sh
pnpm exec vitest run --config test/vitest.config.ts test/dialogs/voice-command-dialog/show-ha-voice-command-dialog.test.ts test/habab/native-ui-bridge.test.ts test/data/recorder-info.test.ts
```

The browser integration and wheel installation checks are performed by the
parent release workflow.
