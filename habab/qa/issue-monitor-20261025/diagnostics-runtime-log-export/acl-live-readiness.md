# Issue #16: bounded live ACL smoke

Status: **PASS_BOUNDED_LIVE_ACL_SMOKE**. The original inline attempt remains **UNVERIFIED** because it did not retain an exact UUID receipt. A single corrected smoke then passed on owner-test 8395 in 0.49 seconds: non-admin list/export returned 403, the promoted account's own list was empty, foreign session detail returned 404, and exact fixture deletion restored the original user-set digest; fixture token revocation also succeeded. Its sanitized receipt is `acl-live-smoke-2026-10-25T20-38-41-106Z.json` (mode `0600`). This is live ACL evidence for the listed checks, not full Issue #16 acceptance.

## Harness

`acl-live-smoke.cjs` is the bounded owner-test runner. It refuses live requests unless invoked with `--run-live` and is hard-bound to loopback owner-test port `8395` plus the canonical owner auth file. It ran once after independent source review.

The runner:

- validates canonical auth file permissions and a matched `auth/current_user` owner/admin result before mutating users;
- records the initial user-set digest and fixture-name SHA-256 before creation, then reads the `config/auth/create` contract as `{user: User}` and checkpoints only `user.id`'s SHA-256;
- creates one local-only `system-users` QA account with credentials generated in RAM; if the create response is lost, recovery is limited to the unique new local non-owner, non-system account matching the exact pre-recorded fixture name;
- verifies non-admin session-list and export denial, promotes that same UUID, verifies its own list is empty and a pre-existing foreign session detail returns 404;
- writes a sanitized mode-0600 receipt atomically before cleanup and from `finally`, recording only the created UUID hash, before/after user-set digests, statuses, and cleanup proof;
- attempts fixture-token revocation in RAM and records its result; Home Assistant user deletion is authoritative for attached credentials and tokens, then the runner confirms that the exact UUID is absent and the user-set digest returned to its baseline;
- on SIGINT or SIGTERM, waits for the current bounded request, stops before the next stage, and runs the same exact-UUID cleanup path.

The receipt contains no names, UUIDs, access or refresh tokens, passwords, session IDs, response bodies, or exported archive bytes. If the UUID cannot be captured or exact deletion and readback cannot be confirmed, cleanup and the run remain **UNVERIFIED**.

Focused receipt/digest tests are in `acl-live-smoke.test.cjs`. They test local receipt logic only and do not establish live ACL behavior. No new live call was made while adding the helper.
