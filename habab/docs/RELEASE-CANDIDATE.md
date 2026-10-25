# Habab release candidate workflow

`.github/workflows/habab-release-candidate.yaml` is a manual, non-publishing
candidate workflow. It accepts only a full Git commit SHA or an existing tag,
resolves that input once, and fetches only the required source paths at the
resulting full SHA. The resolver fetches commit metadata with `blob:none` to
check ancestry against current `origin/main`; platform jobs use shallow,
blob-filtered sparse checkouts. Choose `all`, `windows`, `linux`, or `android`
to control which targets are included in the final manifest. It never pushes
an image, updates `main`/`latest`, uploads to the public download host, or
creates a GitHub Release. Candidate artifacts stay in GitHub Actions for one
day.

## Targets

- **Windows:** the existing builder's installer kit ZIP from the selected
  commit. Its
  `windows-package.json` contains the ZIP digest, size, and member allowlist;
  the candidate step verifies embedded provenance and compares every ZIP
  member with the selected Git tree. The builder's bootstrap BAT files pin the
  candidate filename and digest, and become usable once the candidate is
  separately promoted to their configured download host.
- **Linux amd64 and arm64:** separate OCI image builds and artifacts. The
  workflow matrix does not hide one architecture's failure behind the other's
  result. Each image receives `org.opencontainers.image.revision` and
  `io.habab.source.dirty=false` build labels.
- **Android:** `all` records Android as blocked; it does not run Android or
  expose signing secrets. Choose `android` explicitly to request the Android
  target, then provide an HTTPS runtime-manifest URL and configure all four
  repository Actions secrets below. Missing inputs make that selected job fail
  and prevent a manifest from being assembled. The signed runtime manifest must
  attest `sourceRevision` equal to the selected source SHA; the workflow checks
  `sourceFiles` against that Git tree and verifies its signature, archive hash,
  and contents with the checked-in trusted key before building the APK. It also
  checks the signed `nodePackages` inventory against the selected source's
  Node APK lock, including the builder's `icu-data-en` exclusion when network
  tools are enabled. The runtime producer accepts `--source-revision` and signs the value into the
  manifest. Android keystore secrets are exposed only to the signing step after
  Gradle checks finish. A debug APK or runtime for another commit is never used.

`release-candidate.json` is assembled only after every selected build job
succeeds. It lists only the requested scope's targets; `all` includes Windows,
both Linux architectures, and an explicit blocked Android status. The manifest
records the common source SHA, `source_dirty=false`, and SHA-256 plus byte
count for each payload, alongside its original target-artifact name. The final
artifact contains only this small manifest; platform payloads remain in their
original per-target artifacts. Artifact creation is not a public release or
physical-device acceptance.

## Run a candidate

1. Open **Actions → Habab release candidate → Run workflow** and choose the
   target scope. Use `windows` for a small Windows-only CI proof; `all` builds
   Windows and both Linux images while recording Android as blocked.
2. Enter a full commit SHA, or a tag name that already exists. Branch names and
   ref paths are rejected. The selected commit must also be an ancestor of the
   current `origin/main`, which is the workflow's trusted-source gate.
3. Select `android` only when a signed runtime from the exact SHA and all
   Android keystore secrets are ready. Provide the HTTPS URL for
   the manifest under `habab-demo.wpmix.net/downloads/android/`. Its signature
   is expected at `<manifest URL>.sig`; its signed `url` field identifies the
   runtime archive under the same canonical path.
4. Download the final `habab-release-candidate-<SHA>` manifest artifact and
   inspect `release-candidate.json`. Its `artifact` fields name the separate
   per-target artifacts containing the payloads.

Android repository Actions secrets:

| Secret | Value | Used for |
| --- | --- | --- |
| `HABAB_ANDROID_KEYSTORE_BASE64` | Base64 encoded Android release keystore | Signs the release APK |
| `HABAB_ANDROID_KEYSTORE_PASSWORD` | Keystore password | Opens the keystore |
| `HABAB_ANDROID_KEY_ALIAS` | Signing key alias | Selects the APK signing key |
| `HABAB_ANDROID_KEY_PASSWORD` | Signing key password | Signs the APK |

Do not add these values to workflow inputs or repository files. The Android
job deletes its temporary keystore after signing. The workflow does not create
or rotate keys and has no runtime publishing credentials.

## Failure and recovery

If a selected target fails, its job is failed and the final manifest is not assembled.
The Linux architecture jobs remain independently visible. Correct
the source or the missing input and rerun the workflow against the same SHA.
Failed and older candidate artifacts expire with the Actions retention window;
no public pointer has been changed. Before promotion, compare every artifact
hash in the manifest and perform platform installation, upgrade/rollback, and
physical Android checks separately.
