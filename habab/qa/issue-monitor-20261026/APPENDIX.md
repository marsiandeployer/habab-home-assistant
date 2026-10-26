# QA appendix — 2026-10-26

These bounded receipts add evidence to the open issue board. They do not close issues or establish complete release acceptance.

## #5 Telegram worker readiness

Source commit `805dbefc8` clears worker readiness on fatal poll errors and reader termination; the fake regression covers fatal error and EOF (`1/1`). The owner overlay receipt at source `59291f7f2a620fb696438ad027e0d0702f6b0de9` confirms the `habab_telegram` integration loaded after restart and records an admin owner. It does not confirm a live Telegram poller or current Telegram readiness; see [sanitized overlay receipt](owner-diagnostics-telegram-overlay.json).

## #16 Combined diagnostics export

One owner text turn completed and the selected-conversation export returned HTTP 200. The ZIP contained 17 members (98,291 bytes uncompressed), including shared OpenCode and Home Assistant log tails. The archive manifest marked the sources as instance-wide, `period.filter_status=not_requested`, and the bounded logs as truncated; the temporary ZIP was deleted. The receipt contains counts and manifest fields, not history or log contents. This single export does not establish full issue acceptance; see [combined export receipt](diagnostics-combined-owner-e2e.json).

## #14 CI candidate

Workflow run `40254665226` used source `59291f7f2a620fb696438ad027e0d0702f6b0de9`. Windows, Linux amd64, and Linux arm64 candidate jobs built; Android was blocked because its job was skipped. The four uploaded artifacts have one-day retention. This is candidate workflow evidence, not a public release, device installation, or physical acceptance; see [CI candidate receipt](ci-candidate-59291.json).

## #2 Inventory answer

One owner-session inventory question completed with one read-only `devices` tool action and four returned rows (4 of 4; trace not truncated). Registry readback remained unchanged: three rows were `service` entries and one had a null type. The receipt does not infer physical device count or assign a specific type to the null entry. This is one semantic case, not complete Assist, Windows, phone, or physical-device acceptance; see [inventory receipt](inventory-owner-actual-e2e.json).

## #10 Private native frontend build

The isolated native frontend 0.10 build completed from source `59291f7f2a620fb696438ad027e0d0702f6b0de9`, with patch composition and all 32 changed-path snapshots matching the recorded upstream reconstruction. Wheel metadata, branding, attribution, required fonts, and wheel hash/size checks passed. Retention was skipped because no dedicated state exists; no public release, owner install, or browser acceptance occurred. See [private build receipt](native10-private-build.json).
