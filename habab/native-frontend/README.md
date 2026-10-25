# Matched Home Assistant frontend bridge

This patch targets Home Assistant frontend tag `20260930.4`, commit
`26690e06b0bf64a1487929245c32842ef92c62b5`. The released `habab.7` patch adds
the admin-only assistant handoff, the versioned `window.hababNativeUi` bridge,
and Home favorites that remain visible without `usage_prediction`. Vacuum
favorites use a full-width native Home tile with supported start/pause, stop, and
return-home controls. A Home data subscription refreshes the open panel when
favorites change, without replacing the user's saved Home settings. The native
dialog fallback remains available.

Released `.7` patch SHA-256: `06b9ddd22e5ab57ac625f62c856afbba9f780e5e5ca9177da19afcf36c4267bb`.

## `.8` promoted release and `.9` source candidate

`distribution/native-frontend-release.json` and the current owner runtime use
the `.8` wheel below. The current `bridge.patch` is an unbuilt `.9` source
candidate with SHA-256
`d483686b341c34705a62ca98f7358023599bdb92c80b039b005d4eeef472e092`.
`build-wheel.sh` targets `20260930.4+habab.9` in a fresh
`habab-native-frontend9-build` worktree. The candidate keeps the dirty-state
reload provider and moves the native UI bridge import into the app entrypoint,
so the bridge and its lazy native-form imports have one app runtime owner. The
build adds a bounded path for the modern and legacy bundles. `.9` has not been
built, published, or checked in an owner browser.

The `.8` wheel was built privately as
`home_assistant_frontend-20260930.4+habab.8-py3-none-any.whl`: 111,324,155 bytes,
SHA-256 `8aff0182e0665185edd42ff1659bc68247f39acc998f75f8232c6be750ef9f9a`.
Independent readback passed SHA-256, ZIP CRC, and wheel metadata checks. It found
1,134 modern JavaScript files and 1,165 legacy files; both include the shared
reload provider. The wheel contains 12 WOFF2 font assets. The focused 67-test run
under Node 24 and build preparation/type checks passed; font checks passed and
the Russian catalog fingerprint remains
`154cc8ea8822c85d63b46e34ccc667cbac01df2cbf8cacb0a300bfe1fecda3ce`.
The independent wheel readback is recorded in
[`native-frontend-wheel8-independent-review.json`](../qa/issue-monitor-20261024/native-frontend-wheel8-independent-review.json).

This private build used cached pinned dependencies and direct package binaries.
Modern and legacy bundles were built sequentially with Terser concurrency 1 in a
5 GiB cgroup. The runner used a three-line offline `build-translations`
preparation, so the result does not prove that an unmodified canonical builder
reproduces the wheel. The license asset was reused byte-for-byte from `.7` because
the `gen-licenses` cache index was unavailable; license generation was not rerun.

The immutable `.8` artifact is available on the artifact server, with its full
HTTPS body verified against the digest and size. It remains the promoted
manifest version and is installed in the current owner runtime. A local
candidate image was built from clean commit
`f803277c68511622e3ad780d10a8a046cb9ef478` on branch
`qa/owner-frontend8-20261025`: all 44 component files matched and 80 baked backend
tests passed without a source overlay. See
[`frontend8-candidate-image-independent.json`](../qa/issue-monitor-20261024/frontend8-candidate-image-independent.json).
Build and archive readback are not live acceptance. The release manifest stays
on `.8` until a later candidate is built and promoted.

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
  /root/.local/share/habab-native-frontend9-build \
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
`home_assistant_frontend-20260930.4+habab.9-py3-none-any.whl`.

The wheel build runs `rspack-prod-app-modern` followed by
`rspack-prod-app-legacy` so the two compiler configurations do not overlap.
`HABAB_TERSER_WORKERS=1` limits each pass to one worker. Other frontend builds
keep the existing automatic Terser parallelism unless they set this option.
`SKIP_FETCH_NIGHTLY_TRANSLATIONS=1` applies to all translation stages; the
builder runs the normal license generation step and does not reuse a prior
wheel's license output.

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
release or physical-device acceptance. The `.8` private wheel build and archive
checks are recorded above; public release and owner-installation acceptance
remain pending.

The browser integration and wheel installation checks are performed by the
parent release workflow.
