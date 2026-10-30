# Habab release candidate workflow

`.github/workflows/habab-release-candidate.yaml` is a manual, non-publishing
candidate workflow. It accepts only a full Git commit SHA or an existing tag,
resolves that input once, and fetches only the required source paths at the
resulting full SHA. The resolver fetches commit metadata with `blob:none` to
check ancestry against current `origin/main`; platform jobs use shallow,
blob-filtered sparse checkouts. Choose `all`, `windows`, `linux`, `android`, or
`android-runtime` for the separate unsigned runtime review path. It never pushes
an image, updates `main`/`latest`, uploads to the public download host, or
creates a GitHub Release. Candidate artifacts stay in GitHub Actions for one
day.

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
WebSocket) inside its existing bounded HA test container before compiling any
Android frontend. Do not repeat a completed build, archive scan, or device
runtime installation without changed inputs, a failure, or a concrete concern.
Publication still checks signatures and artifact hashes; device acceptance is
reported separately from source/build checks.

APK native `.deb` inputs are served by SHA-256 from
`https://habab-demo.wpmix.net/downloads/android/native-inputs/<SHA256>.deb`.
The producer still verifies every file against `native-packages.json` and
includes the upstream notices. Publish the verified pinned inputs before changing
that lock: upstream Termux repositories remove older package revisions. These
small shared build dependencies remain available while a retained APK release
uses them; they are not a replacement for a signed server runtime.


## Targets

- **Windows:** the existing builder's installer kit ZIP from the selected
  commit. Its
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
  calls the existing native frontend compiler at the selected source SHA,
  downloads its actual wheel and provenance manifest, packages a fresh ARM64
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
  retains its sourceFiles, launcher, configuration and Node APK inventory checks,
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

This is a source implementation. At the 2026-10-26 read-only configuration check,
this repository had no Actions secrets configured. No frontend/runtime/APK build,
workflow dispatch, publication, or physical Android acceptance was performed for
this change. Configure the five signing secrets, select explicit release version inputs,
and review the exact source commit before running an Android candidate. Local proof covers small synthetic
provenance, gate, archive and packaging tests; `actionlint` was not available and
was **NOT RUN**. The candidate artifacts still expire after one day; normal
publication remains a separate operator step with the existing current/previous
release retention procedure in `habab/android/README.md`.

## Failure and recovery

If a selected target fails, its job is failed and the final manifest is not assembled.
The Linux architecture jobs remain independently visible. Correct
the source or the missing input and rerun the workflow against the same SHA.
Failed and older candidate artifacts expire with the Actions retention window;
no public pointer has been changed. Before promotion, compare every artifact
hash in the manifest and perform platform installation, upgrade/rollback, and
physical Android checks separately.
