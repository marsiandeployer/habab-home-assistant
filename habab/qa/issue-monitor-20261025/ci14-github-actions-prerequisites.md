# Issue 14 GitHub Actions prerequisites

Read-only check, 2026-10-25. No dispatch, run, build, push, billing change, or
secret-value read was performed.

## Repository and dispatch

- **PASS:** `marsiandeployer/habab-home-assistant` is public, enabled, and
  uses `main` as its default branch.
- **PASS:** Repository Actions are enabled; allowed actions are `all`;
  default `GITHUB_TOKEN` workflow permissions are read-only. Repository
  artifact/log retention allows up to 90 days; workflow retention is 14 days.
- **PASS:** Auth response scope metadata includes `workflow`; repository
  permission metadata reports admin access. The token itself was not printed.
- **BLOCKED:** The current remote `main` is
  `0177dcdedd41b478675afc2616e1d09957136d0b`. Its workflow list has no
  `habab-release-candidate.yaml`, and the default-branch contents endpoint
  returns 404. The local workflow must be committed and pushed to `main`
  before dispatch can work.

After that push, and only after storage headroom is confirmed, the exact
non-Android dispatch command is:

```sh
gh workflow run habab-release-candidate.yaml \
  --repo marsiandeployer/habab-home-assistant \
  --ref main \
  -f source_ref=<FULL_SHA_ON_MAIN> \
  -f include_android=false
```

## Runner and storage

- **PASS:** The repo is public and the workflow uses standard `ubuntu-latest`
  runners. GitHub says standard hosted runners are free for public repos.
- **PASS:** Actions artifact API reports zero current artifacts; cache usage
  reports zero active cache bytes.
- **BLOCKED:** Account plan and shared Actions/GitHub Packages storage headroom
  are unknown. The repository billing endpoint returned 404. The documented
  personal billing usage endpoint returned 404 with GitHub's message that the
  current OAuth token needs the `user` scope. Do not upload the OCI candidates
  until this is resolved.
- **Storage risk:** The two Linux OCI job artifacts are expected to be about
  2.7 GB each. The workflow then uploads them again inside the merged candidate
  artifact, so peak retained Actions artifacts may approach 10.8 GB for the
  configured 14-day retention. GitHub's included shared artifact/Packages
  storage is 500 MB on Free, 1 GB on Pro, 2 GB on Team, and 50 GB on Enterprise
  Cloud; the account plan and Packages usage were not accessible. Runner time
  being free does not establish artifact-storage headroom.

The documented retry after user approval for the missing OAuth scope is:

```sh
gh auth refresh -h github.com -s user
gh api 'users/marsiandeployer/settings/billing/usage/summary?year=2026&product=actions'
gh api 'users/marsiandeployer/settings/billing/usage/summary?year=2026&product=packages'
```

This only obtains usage data; it does not add storage, change billing, or
authorize a dispatch. If the endpoint still returns 403/404, check the
repository owner's GitHub billing settings before proceeding.

Official references: [Actions billing and shared storage](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [public hosted-runner policy](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job), and [billing usage API](https://docs.github.com/en/rest/billing/usage).

## Android secrets

The repository Actions secrets metadata endpoint returned `total_count=0`;
no values were requested. The workflow's optional Android path requires
`HABAB_ANDROID_KEYSTORE_BASE64`, `HABAB_ANDROID_KEYSTORE_PASSWORD`,
`HABAB_ANDROID_KEY_ALIAS`, and `HABAB_ANDROID_KEY_PASSWORD`. Android remains
off by default; no secrets are needed for the non-Android candidate.
