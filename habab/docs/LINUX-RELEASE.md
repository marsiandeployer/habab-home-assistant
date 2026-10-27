# Linux packages: native installation and Docker

Habab Home has two independent Linux deliverables:

- Native ARM64 installer for Ubuntu Server 26.04, without Docker. It installs
  Home Assistant Core into a versioned Python environment and runs systemd.
- Docker image for linux/amd64 and linux/arm64, with OpenCode embedded. The
  release Compose file uses a persistent `/config` volume.

These release workflows extend issue #14. Its existing `linux` candidate target
still means OCI archives; it does not produce the native installer or publish an
image to a registry. Issues #29 and #30 track the two deliverables separately.

## Build candidates

Choose a full source commit on `main` and an unused Core-matched frontend wheel
version. Both builders call the existing native frontend compiler for that exact
commit. Neither consumes a previously published frontend wheel.

```sh
gh workflow run habab-native-linux-release.yaml \
  -f source_sha=<full-source-sha> -f wheel_version=20260930.4+habab.<N>

gh workflow run habab-docker-release.yaml \
  -f source_ref=<full-source-sha> -f image_tag=<versioned-tag> \
  -f frontend_wheel_version=20260930.4+habab.<N> -f publish=false
```

The native ARM64 Actions VM installs the package without Docker, creates a test
owner, checks authenticated product status and restarts systemd with preserved
configuration. The Docker job checks both platform images in bounded containers.
Read the actual smoke receipts; a workflow definition is not a passed check.
Candidate artifacts expire after one day.

## Publish tested artifacts

Publication downloads the successful candidate's existing artifact and verifies
its source, hashes and smoke receipts. It does not rebuild the application.

```sh
gh workflow run habab-native-linux-promote.yaml \
  -f candidate_run_id=<successful-native-run> -f source_sha=<full-source-sha>

gh workflow run habab-docker-promote.yaml \
  -f candidate_run_id=<successful-docker-run> -f source_sha=<full-source-sha>
```

Native packages are separate GitHub prereleases. Docker images go to
`ghcr.io/marsiandeployer/habab-home-assistant`; package visibility must permit
anonymous pulls before the site offers the image as a public download. Publishing
uses the workflow's `GITHUB_TOKEN`, with `contents: write` for native releases and
`packages: write` for images.

After public readback, update the matching entry in habab.ru's
`content/products/ai_home_server/downloads.yaml` and the installation articles.
Publish the exact package URL/checksum or image tag/digest from the receipt.

## Acceptance still needed

VM/container checks do not prove Raspberry Pi hardware, a physical reboot,
USB/Zigbee operation, voice, or a real provider conversation. Native configuration
is separate from release files; successful installation retains the current and
previous application versions. Back up configuration before updating Core: an
application rollback does not reverse a database migration.
