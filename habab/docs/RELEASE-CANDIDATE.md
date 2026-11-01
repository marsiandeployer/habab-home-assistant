# Habab release candidate workflow

`.github/workflows/habab-release-candidate.yaml` is a manual, non-publishing
candidate workflow, also reused by the public release workflow. It accepts only a full Git commit SHA or an existing tag,
resolves that input once, and fetches only the required source paths at the
resulting full SHA. The resolver fetches commit metadata with `blob:none` to
check ancestry against current `origin/main`; platform jobs use shallow,
blob-filtered sparse checkouts. Choose `all`, `windows`, `linux`, `android`, or
`android-runtime` for the separate unsigned runtime review path. It never pushes
an image, updates `main`/`latest`, uploads to the public download host, or
creates a GitHub Release. Candidate artifacts stay in GitHub Actions for one
day.

## Core API checks on commits

`.github/workflows/habab-core-e2e.yaml` runs the same backend suites and real
Core API acceptance runner on pushes to `main`, pull requests touching
`habab/**` or these workflow files, and manual dispatch. It uses the release
preflight's pinned Home Assistant image in one isolated container, with read-only
source mounts, 2 GiB memory, two CPUs and a 256-process limit. The timeout is
20 minutes. Its temporary Docker bridge is created with `--internal`, which
provides a real network interface without external egress; the step verifies
`Internal=true`. Cleanup stops its own container before removing its own network.
Five harness contract checks run before the container in this
workflow and the release preflight. Tests use disposable local configuration and
a local provider fixture. No provider secrets or public model requests are needed. Only a newer
check for the same PR cancels its previous check; pushes and manual checks keep
separate run identities. This workflow does not publish release assets.

## Publish all platforms to GitHub

`.github/workflows/habab-release.yaml` is the public release entrypoint. Dispatch
it from `main` at the frozen source commit with four explicit inputs:

```sh
gh workflow run habab-release.yaml --ref main \
  -f source_revision=<FULL_MAIN_SHA> \
  -f release_tag=habab-v<VERSION>-<FIRST_12_SHA_CHARACTERS> \
  -f android_version_code=<NEW_CODE> \
  -f android_version_name=<REVIEWED_NAME>
```

The product SHA is frozen independently of the controller workflow SHA. They
normally match. A corrected controller may target an earlier product commit
only when every changed path in every commit of the complete product-to-controller
range is in the narrow workflow/release-helper/test/docs allowlist. Intermediate
product edits followed by a revert still fail this check. All platform payloads
and signatures continue to bind the frozen product SHA. The publisher executes
the corrected controller helper and verifies a separate checkout of that product.
Both identities are recorded in the release receipt. The tag includes the
product SHA's first 12 characters. An absent tag can be created; an existing
lightweight tag is accepted only if its full target SHA equals the frozen product.
Annotated tags and different targets fail. This permits an operator to prepare
the exact tag when CI cannot create the frozen-product ref. The publisher
rechecks the target and skips tag creation when it already matches. Existing
releases or drafts are rejected using the full paginated inventory, including a
second check with the publisher token. Tags and release assets are never overwritten. The workflow serializes public releases and requires all
five existing Android signing secrets before starting platform builds.

For a CI-only retry, optional `frontend_run_id` reuses a completed frontend
artifact from the same repository's trusted manual producer workflow at the
exact product SHA. For example, the verified frontend from run `41282055361`
belongs to product `f5ac8458dd1d05084035cb7ac24b4ae8fb9a8c28`. Its original
manifest and producer identity remain unchanged. The artifact's exact wheel
bytes, version and manifest source are rechecked before runtime packaging, and
the original artifact run ID is recorded. The frontend compilation job is
skipped; Windows/Linux builds and runtime/APK packaging continue. A prior signed
runtime that was never uploaded by its failed job cannot be recovered from the
completed hosted runner and must be rebuilt.

For a completed whole-candidate retry, set `candidate_run_id`, for example
`41285741688` for product `f5ac8458dd1d05084035cb7ac24b4ae8fb9a8c28`.
Validation checks the trusted public workflow's run ID, attempt and controller
SHA against the original aggregate and every target status, requires successful
Windows/Android/both-Linux/aggregate jobs, and checks the requested APK version.
The candidate build job is skipped. The publisher downloads those exact platform
artifacts and aggregate from the producer run, then repeats the existing payload,
source, RSA signature and checksum gates. The aggregate's bytes are pinned across
validation and publication. Its original `workflow_revision`, run ID and attempt
remain producer metadata; `publisher_workflow_revision` records the current
publisher separately. The frozen product checkout includes `habab/product-tunnel`
for strict Windows ZIP source verification. No platform is rebuilt or re-signed.

The release reuses the candidate producer with `target_scope=all`. This caller
requires a built Windows EXE, both Linux OCI images, and a signed public Android
APK/runtime. A blocked, skipped, unsigned, failed, or missing platform cannot
reach publication. Review-only candidate dispatches retain their existing
explicit Android `blocked` and unsigned-runtime behavior.

The download assets are:

| Platform | GitHub asset |
| --- | --- |
| Windows | `habab_home_assistant.exe`, the source-bound ZIP, `windows-package.json` |
| Android | `habab-home-arm64.apk`, `release-<ID>.json`, its `.sig`, and the signed runtime archive |
| Linux amd64 | `habab-home-linux-amd64.oci.tar.gz` |
| Linux arm64 | `habab-home-linux-arm64.oci.tar.gz` |
| Provenance | `release-receipt.json`, `SHA256SUMS` |

Windows runs the existing ZIP packager and Inno Setup compiler on
`windows-latest`; `HABAB_WINDOWS_PUBLIC_BASE` points at the immutable GitHub tag.
The EXE embeds its installation payload. Linux keeps the existing OCI builder
and compresses the resulting archive with `gzip --no-name`; each architecture's
OCI descriptors, layers, revision label and clean-source label are verified.
Each Linux image embeds the integrity-pinned OpenCode executable and is booted
on its target architecture for owner-onboarding, API and restart smoke checks.
Windows continues to use its read-only OpenCode volume.
The common source SHA binds distribution code and its dependency pins. The
Windows/Linux Docker builder installs the frontend wheel pinned in
`native-frontend-release.json`; it does not freshly compile that frontend. The
receipt records this dependency and its original producer source separately
from the new distribution/backend source SHA. The current pin is the verified
`20260930.4+habab.20` wheel from producer commit `224fc2adbff66e42fa27d69945cb8dcc0f79c93c`
and CI run `41223227286`. Android compiles a fresh frontend from the release
source as described below. Before publication, the pinned Windows/Linux wheel
SHA-256 must equal `nativeFrontend.wheelSha256` in the signed Android runtime;
different frontend bytes fail the release gate.
Every published asset must be nonempty and smaller than GitHub's 2 GiB limit.
Oversized output fails before a draft is created.

The Android producer receives this exact repository/tag's GitHub download base.
The signed manifest's runtime URL and the APK's embedded default URL/provenance
must identify those assets. The current runtime is freshly built and signed;
there is no external factory-runtime dependency in this release path. Existing
runtime RSA and APK signing identities are retained. CI prepares the pinned
public Vosk model; it does not stage private experimental wake models. Both the
source-asset check and the final APK check reject experimental model weights and
WAV recordings. Private APK57 and local model/audio packs are never inputs.

Android's `ReleaseDownload` helper follows at most five HTTPS redirects. A
GitHub download may redirect to `release-assets.githubusercontent.com`, including
its short-lived signed query. Other cross-host redirects, HTTP downgrades,
credentials, fragments and nonstandard ports fail. Runtime manifest signatures,
archive size and archive SHA-256 checks still run before installation.

Before upload, `github-release.py` rechecks candidate checksums, common source
and workflow identities, Windows installer metadata, runtime RSA signature,
APK runtime/frontend provenance, private-model exclusion, and both OCI payloads.
It stages an explicit asset list and creates a draft. After upload it compares
GitHub's actual asset `digest` (SHA-256), byte count, state, and full inventory
with the local receipt. This proves uploaded-byte identity using GitHub's
calculated checksum; it does not re-download all multi-gigabyte payloads.
Only after this comparison does the workflow publish the draft and mark the
release latest. It then downloads and byte-compares the small public receipt and
performs redirected HEAD requests for every direct asset URL.

Website download buttons can use:

```text
https://github.com/marsiandeployer/habab-home-assistant/releases/latest/download/habab_home_assistant.exe
https://github.com/marsiandeployer/habab-home-assistant/releases/latest/download/habab-home-arm64.apk
https://github.com/marsiandeployer/habab-home-assistant/releases/latest/download/habab-home-linux-amd64.oci.tar.gz
https://github.com/marsiandeployer/habab-home-assistant/releases/latest/download/habab-home-linux-arm64.oci.tar.gz
```

The receipt records immutable tag URLs. Release creation/build checks and public
download availability are separate from Windows installation and physical
Android acceptance. A source build still depends on the pinned upstream Alpine
packages being available; Dockerfiles currently pin Node `24.22.1-r1`.

If upload or checksum verification fails, the release remains a draft. Inspect
the draft and Actions artifacts before any cleanup or retry. Do not overwrite a
published version. For rollback, point `latest` and the website back at an
already verified earlier release; do not rebuild or replace its assets. Android
updates still require a greater versionCode and their own compatibility review.

## Choose the smallest release

| Changed component | Release path |
| --- | --- |
| Server relay | Test/deploy the relay only; leave Android unchanged |
| Android Java, resources, or app manifest | APK-only candidate with a compatible existing signed runtime |
| Native frontend, HA components, runtime launcher, or dependencies | Full Android path below |
| Unknown or incompatible inputs | Full Android path; do not guess compatibility |

The APK-only workflow is `.github/workflows/habab-android-apk-candidate.yaml`.
It takes an explicit full `apk_source_revision`, `runtime_manifest_url`,
`android_version_code`, and `android_version_name`. It preserves the existing
runtime/frontend identities and separately records the new APK source revision.
Without all four APK-signing secrets it produces an explicitly unsigned review
candidate; it does not publish or install it. The runtime archive is not
downloaded, rebuilt, or installed in this path. Gradle dependencies use the
existing setup-gradle cache. Reuse requires checking the complete change range from
the runtime source, not just the latest commit. A version label alone is not
proof of compatibility. The existing full workflow remains the conservative
path when runtime/frontend inputs change.

Run the changed component's fast checks before a large build. The full workflow's
preflight now runs the relay tests (including concurrent HTTP with an open
WebSocket) and the real Core API acceptance runner inside its existing bounded
HA test container before compiling any Android frontend. The same container
uses an ephemeral internal Docker bridge and has a 2 GiB memory limit, two CPUs
and 256-process limit. The Core
runner creates disposable configuration and uses a local provider fixture for
onboarding, authentication, provider save/check/reload and a virtual-helper
service cycle. Do not repeat a completed build, archive scan, or device
runtime installation without changed inputs, a failure, or a concrete concern.
Publication still checks signatures and artifact hashes; device acceptance is
reported separately from source/build checks.

The Android job uses `ubuntu-24.04` with explicit host Python `3.14` through the
same pinned official `actions/setup-python` used by the frontend producer,
with `check-latest: false`. It checks the actual `python3` minor version and pip
before packaging; the wheel's Python requirement remains enforced.
Before runtime packaging it verifies the
runner's Java 17 executable and calls SDK manager by the absolute path
`$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager`; an unset `ANDROID_HOME`
falls back to `ANDROID_SDK_ROOT`. A missing tool fails before rootfs work.
It explicitly ensures `platforms;android-34`, `build-tools;34.0.0`,
`ndk;27.2.12479018`, and `cmake;3.22.1` through Google's SDK manager. Signing,
alignment and APK version readback use Build Tools `34.0.0` explicitly. NDK
revision readback and the three NDK environment aliases select `27.2.12479018`
consistently with the product's Gradle `ndkVersion`.
The preinstalled paths and SDK package inventory are documented by the
[official runner image](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
and its [SDK installation script](https://github.com/actions/runner-images/blob/main/images/ubuntu/scripts/build/install-android-sdk.sh).
The workflow uses the [official SDK manager package installation syntax](https://developer.android.com/tools/sdkmanager).

Native `.deb` inputs
download directly from `https://packages.termux.dev/apt/termux-main/` using the
exact `Filename` and SHA-256 in `native-packages.json`. The current PRoot pin is
`5.1.107.101`. The producer verifies every package and includes upstream notices.
The public Vosk model downloads from its locked official `alphacephei.com` URL
and retains its size/SHA-256 check. These producers have no Habab mirror fallback.

Android runtime Node APKs download directly from the official Alpine
`v3.24/main/aarch64` repository. The lock now pins Node `24.22.1-r1` with
`ada-libs 4.0.0-r0`, which provides its required `libada.so.4`; the other 19
packages retain their verified versions and SHA-256 values. The lock records
exact URLs, full archive SHA-256/bytes, official APKINDEX control checksums, and
the reviewed index/key fingerprints. Original runtime-derived metadata is
labelled as the baseline rather than as provenance for the updated transaction.

Before frontend compilation, the input checker fetches the official APKINDEX
and its public key from `alpinelinux.org/keys`, verifies the pinned key
fingerprint and index signature, checks every locked package against that signed
index, verifies the complete dependency closure, and checks all 21 official
package URLs and sizes. The same early checker verifies availability of the
four locked Termux `.deb` URLs and the official Vosk model URL/size. Downloads
still undergo full archive byte/SHA-256 checks and the existing signed APK
installation transaction. No Habab package mirror
is used for these Alpine dependencies.

Upstream repositories can remove older versions. An unavailable pin fails the
early check and requires a reviewed official-source lock update before rebuilding.


## Targets

- **Windows:** the existing builder's installer kit ZIP and compiled
  `habab_home_assistant.exe` from the selected commit. Its
  `windows-package.json` contains the ZIP digest, size, and member allowlist;
  the candidate step verifies embedded provenance and compares every ZIP
  member with the selected Git tree. The builder's bootstrap BAT files pin the
  candidate filename and digest, and become usable once the candidate is
  separately promoted to their configured download host. The ZIP includes the
  local product-tunnel client scripts and non-secret example configuration for
  manual provisioning. The client requires a completed `home.json`, a per-home
  private key, pinned relay host key, local Home Assistant HTTP, and an already
  provisioned relay/DNS/TLS route. The installer does not activate remote access,
  enroll accounts, or connect to billing.
- **Linux amd64 and arm64:** separate OCI image builds and artifacts. The
  workflow matrix does not hide one architecture's failure behind the other's
  result. Each image receives `org.opencontainers.image.revision` and
  `io.habab.source.dirty=false` build labels.
- **Android:** prerequisites are checked before any frontend build. With all
  explicitly reviewed Android version code/name, APK signing secrets and
  `HABAB_ANDROID_RUNTIME_SIGNING_KEY_BASE64`, the workflow
  calls the existing native frontend compiler at the selected product SHA, or
  reuses its verified same-product artifact for a CI-only retry. It downloads the
  actual wheel and original provenance manifest, packages a fresh ARM64
  runtime using that local wheel, and signs it with the existing trusted runtime
  RSA key. The derived public key must equal the checked-in trusted key; no key
  is generated or rotated. The wheel remains content addressed by SHA-256 in its
  frontend artifact; the runtime archive and manifest use their existing
  immutable identity.

  The runtime's signed `nativeFrontend` binds the wheel SHA, producer-manifest
  SHA, frontend source SHA, version, and installed-payload SHA. Verification
  checks both the archive payload and embedded runtime metadata against those
  signed values. The APK embeds `assets/release/build-provenance.json`; its
  actual bytes are read back from the signed APK and checked against this runtime
  and frontend before upload. The embedded `default-url.txt` must equal this
  runtime's canonical `release-<ID>.json` URL; that URL is also recorded in the
  APK provenance and the canonical manifest/signature are included in the
  candidate artifact. A new APK's metadata and URL do not establish that an
  already installed private runtime has been upgraded; that requires separate
  installation/update acceptance. Native tools and the offline voice model are
  prepared through their existing pinned producers before Gradle checks.

  `android_runtime_manifest_url` is optional. If supplied instead of the runtime
  key, that signed runtime must match the **newly built** wheel and producer
  manifest exactly, as well as the selected source tree. A same-version wheel or
  matching backend source alone is insufficient. Old manifests without this
  frontend provenance fail; there is no published-frontend fallback. The workflow
  derives `sourceFiles` through the producer's canonical `copy_sources()` in a
  temporary directory, including component services and bundled license files.
  It retains strict sourceFiles, launcher, configuration and Node APK inventory checks,
  including `.vtt` captions and the network-tools `icu-data-en` exclusion.

  In `all`, missing prerequisites produce an explicit Android `blocked` record
  without building frontend; explicit `android` scope fails for missing inputs.
  Missing Android version code/name follows the same rule; there is no Gradle
  default-version fallback.
  Invalid input, a frontend/compiler failure, runtime mismatch, or APK build/sign
  failure prevents the aggregate manifest. Candidate creation does not publish
  the runtime's download URL, install the APK, or update a phone.
- **Android runtime-only unsigned review candidate:** `target_scope=android-runtime`
  compiles a fresh frontend from the selected SHA, verifies the supplied signed
  base runtime against the checked-in trust anchor and compatible dependency
  pins, then overlays current sources and the fresh wheel without requiring a
  signing secret. The output is a separate one-day Actions artifact containing
  the runtime archive, unsigned manifest, wheel, and producer manifest. Its
  verifier checks the archive and exact source/frontend identity but labels the
  result `unsigned-candidate`; it is not a trusted, installable, or publishable
  runtime and is excluded from the normal release manifest. This path does not
  modify `trusted-key.der` or `default-url.txt`, and skips APK signing/building.
  A reviewed artifact can later be signed locally with the existing runtime key;
  the signed result must pass normal verification and publication/readback checks.

`release-candidate.json` is assembled only after every required target job
succeeds. It lists only the requested scope's targets; `all` includes Windows,
both Linux architectures, and Android as built or explicitly blocked. The manifest
records the common source SHA, `source_dirty=false`, and SHA-256 plus byte
count for each payload, alongside its original target-artifact name. The final
artifact contains only this small manifest; platform payloads remain in their
original per-target artifacts. Artifact creation is not a public release or
physical-device acceptance.

## Run a candidate

1. Open **Actions → Habab release candidate → Run workflow** and choose the
   target scope. Use `windows` for a small Windows-only CI proof. Use
   `android-runtime` only for the separate unsigned runtime review artifact; it
   requires `android_base_runtime_manifest_url`, a signed compatible factory
   manifest under the canonical downloads path, and does not produce an APK.
   `all` builds
   Windows and both Linux images and also builds Android when runtime and APK
   signing secrets are configured; otherwise Android is blocked.
2. Enter a full commit SHA, or a tag name that already exists. Branch names and
   ref paths are rejected. The selected commit must also be an ancestor of the
   current `origin/main`, which is the workflow's trusted-source gate.
3. For Android, supply `android_version_code` and `android_version_name` for
   the reviewed release. Choose a code greater than every distributed/installed
   build being upgraded; the workflow does not invent or default a version.
   Code must be a decimal integer from 1 to 2100000000; name must be 1–64 ASCII
   letters, digits, dots, plus signs, underscores or hyphens, beginning with a
   letter or digit. Gradle receives `-PappVersionCode` and `-PappVersionName`;
   `aapt dump badging` reads the signed APK back and both values must match.
   Configure the existing runtime RSA key and APK signing secrets below. Leave the runtime URL blank for the normal frontend → runtime → APK
   build. A supplied URL must be canonical HTTPS under
   `habab-demo.wpmix.net/downloads/android/`, with a signature at `<URL>.sig`,
   and attest the newly compiled wheel exactly.
   For `android-runtime`, leave the Android version and normal runtime URL inputs
   blank and provide only the signed base manifest URL. The workflow checks free
   disk after downloading that base against the producer's extraction/archive
   space formula before extracting it.
4. For normal scopes, download the final `habab-release-candidate-<SHA>` manifest
   artifact and inspect `release-candidate.json`; its `artifact` fields name the
   separate per-target artifacts containing the payloads. For `android-runtime`,
   download only `candidate-android-runtime-unsigned`; that scope deliberately
   has no aggregate release manifest.

Android repository Actions secrets:

| Secret | Value | Used for |
| --- | --- | --- |
| `HABAB_ANDROID_RUNTIME_SIGNING_KEY_BASE64` | Base64 encoded existing runtime RSA PEM key matching `app/src/main/assets/release/trusted-key.der` | Signs fresh runtime; required when runtime URL is blank |
| `HABAB_ANDROID_KEYSTORE_BASE64` | Base64 encoded Android release keystore | Signs the release APK |
| `HABAB_ANDROID_KEYSTORE_PASSWORD` | Keystore password | Opens the keystore |
| `HABAB_ANDROID_KEY_ALIAS` | Signing key alias | Selects the APK signing key |
| `HABAB_ANDROID_KEY_PASSWORD` | Signing key password | Signs the APK |

Do not add these values to workflow inputs or repository files. The Android
job deletes its temporary runtime private key and APK keystore after signing.
The workflow does not create or rotate keys and has no runtime publishing credentials. Use the existing
operator-owned keys through repository **Settings → Secrets and variables →
Actions**; do not create replacement keys for this workflow.

The read-only inventory on 2026-11-01 confirmed all five Android signing
secret names above, plus `DOCKERHUB_TOKEN`, in repository Actions secrets. The
operator provisioned the existing signing identities; keys were not rotated.
Inspect names with `gh secret list` without printing values. The five signing
secrets are required by the public release workflow.

Local validation for the public release implementation covers synthetic signed
runtime/APK provenance, changed payloads, blocked platforms, private-model
exclusion, OCI archives, uploaded GitHub checksum mismatches, and HTTPS redirect
behavior. `actionlint` 1.7.12 validates both release workflows. A small real
GitHub asset was fetched through the Java helper's GitHub redirect path. These
checks do not claim a completed multi-platform build, public release, Windows
installation, or phone acceptance. Candidate artifacts still expire after one
day; use the current/previous retention procedure in `habab/android/README.md`
for separately managed installed/runtime releases.

## Failure and recovery

If a selected target fails, its job is failed and the final manifest is not assembled.
The Linux architecture jobs remain independently visible. Correct
the source or the missing input and rerun the workflow against the same SHA.
Failed and older candidate artifacts expire with the Actions retention window;
no public pointer has been changed. Before promotion, compare every artifact
hash in the manifest and perform platform installation, upgrade/rollback, and
physical Android checks separately.
