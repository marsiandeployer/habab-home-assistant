# Issue 14 release-candidate readiness

Review base: `9e22386e9a0d64527fadaba5290b5b6758792e11`.

## Source checks

- **PASS:** Workflow is manual-only, resolves one SHA, gates the selected commit as an ancestor of current `origin/main`, and uses read-only repository permissions.
- **PASS:** Windows uses the canonical `package-windows.py` builder. The existing source receipt records ZIP CRC, SHA-256, member-to-source comparisons, bootstrap pins, and `published=false`: [ci-windows-package-contract.json](ci-windows-package-contract.json).
- **PASS:** Android workflow checks the signed runtime's `sourceRevision`, source file inventory, launcher digest, and the seed configuration produced by the documented canonical append. Optional network-tools lock and launcher templates are also source-bound.
- **PASS:** Android documentation passes the required `--source-revision` value and explains the source match.
- **PASS:** Source fetch now filters blobs and checks out only the platform paths; the resolver fetches the selected ref plus `origin/main` commit graph for ancestry verification. The pinned checkout action documents that `filter` overrides its `sparse-checkout` input, so the workflow sets partial-clone and sparse-checkout directly before checkout.
- **PASS:** Per-target candidate artifacts retain the Windows kit, Android package (when built), and Linux OCI images. The final SHA-named artifact contains only `release-candidate.json`; payload checksums, sizes, and original artifact names are recorded without retaining duplicate OCI copies.
- **PASS:** Scoped workflow YAML, Bash, and embedded Python syntax checks were completed in the prior handoff; no workflow was run during this integration review.

## Not run

- Linux amd64/arm64 GitHub Actions matrix: **NOT RUN**.
- Android runtime/APK build and APK signing: **NOT RUN**. Signing secrets were not inspected or used.
- Physical Windows installation: **NOT RUN**.
- Workflow dispatch, public publication, and GitHub Release: **NOT RUN**.
- GitHub artifact storage headroom: **UNKNOWN**; dispatch remains gated on account storage review.

## Commit scope

New files owned by the CI14 change:

- `.github/workflows/habab-release-candidate.yaml`
- `habab/docs/RELEASE-CANDIDATE.md`
- `habab/scripts/release-candidate.py`
- `habab/scripts/test_release_candidate.py`
- `habab/qa/issue-monitor-20261025/ci14-release-candidate-readiness.md`

In `habab/android/README.md`, stage only the first diff hunk that documents
`--source-revision`; leave the separate runtime-publisher/retention hunk with
its owner. Do not include Android worker files in the CI14-owned file list.
