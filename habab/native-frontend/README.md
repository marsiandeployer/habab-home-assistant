# Matched Home Assistant frontend bridge

This patch targets Home Assistant frontend tag `20260930.4`, commit
`26690e06b0bf64a1487929245c32842ef92c62b5`. The released `habab.7` patch adds the admin-only
assistant handoff, the versioned `window.hababNativeUi` bridge, and Home favorites
that remain visible without `usage_prediction`. Vacuum favorites use a full-width
native Home tile with supported start/pause, stop, and return-home controls. A Home data
subscription refreshes the open panel when favorites change, without replacing
the user's saved Home settings. The native dialog fallback remains available.

Released `.7` patch SHA-256: `06b9ddd22e5ab57ac625f62c856afbba9f780e5e5ca9177da19afcf36c4267bb`.

## Pending source candidate

The current `bridge.patch` is a pending `.8` source candidate with SHA-256
`c47ee0a56a56f4b8c260ea3b4db3d6111da22e9a6f7d29b1d428882f50ec4010`.
`build-wheel.sh` targets `20260930.4+habab.8`. The change adds
`window.haRegisterDirtyStateProvider`, backed by `DirtyStateProviderMixin`, so an
external frontend component can register unsaved state with Home Assistant's
service-worker reload guard and defer reload until it is safe.

No `.8` wheel has been built, so there is no `.8` wheel digest or size. Isolated cached-dependency checks passed: ESLint, Prettier, and 67 focused Vitest
tests under Node 22.21.1; real Gulp input generation and the TypeScript check
under Node 24.21.0. The complete builder under pinned Node 24 and the wheel build
remain pending. At this review on 2026-10-25, the filesystem had about 1.5 GiB free. The release
manifest still describes the published `.7` wheel below; `.8` is not a released
artifact.

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
  /root/.local/share/habab-native-frontend-vacuum-build \
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
`home_assistant_frontend-20260930.4+habab.8-py3-none-any.whl`.

The pinned patch also fixes Roboto copying in `gather-static`: the filter keeps
directories and `.woff2` files, so absolute source paths under dotted parents
such as `/root/.local/share/` no longer prune the whole font tree. The wheel
builder checks Regular, Medium, and Bold files, plus at least 12 WOFF2 assets,
in the gathered static directory and again in the packaged wheel. The final
check verifies the WOFF2 signature and byte-for-byte SHA-256/size equality for
the three required files, then writes a sanitized report to
`habab/qa/native-font-assets/font-assets-report.json`.

The catalog check fingerprints all 19 `ru-*.json` files by relative path and
content SHA-256. Expected catalog fingerprint:
`154cc8ea8822c85d63b46e34ccc667cbac01df2cbf8cacb0a300bfe1fecda3ce`.
The artifact directory is keyed by the final wheel SHA-256, matching the
installer URL convention.

The released `.7` wheel is 111,675,760 bytes with SHA-256
`1d518e7cf632827f04481f0ef0d8ad3aec65d17b25cdc73f1b79c34de0ee1867`.
The frontend asset version remains `20260930.4`; `+habab.7` is Python wheel
metadata used to distinguish the bridge build while preserving Core version
compatibility. The `.7` wheel is stored in `habab/public/downloads/native-frontend/`
under a directory named for its final SHA-256; the installer uses that digest
directory.

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

## Home favorites

The native Home overview keeps explicitly pinned entities visible when the
`usage_prediction` integration is unavailable. Vacuum favorites use the
`mdi:robot-vacuum` tile icon and the native `vacuum-commands` feature for
start/pause, stop, and return-home. Home Assistant filters each command against the
entity's supported features.

## Checks

Focused source tests:

```sh
pnpm exec vitest run --config test/vitest.config.ts test/dialogs/voice-command-dialog/show-ha-voice-command-dialog.test.ts test/habab/native-ui-bridge.test.ts test/data/recorder-info.test.ts
```

The `.7` wheel build completed successfully. This does not establish public
release or physical-device acceptance. The `.8` source candidate has passed
the isolated checks described above; its wheel build and full builder gates
remain pending.

The browser integration and wheel installation checks are performed by the
parent release workflow.
