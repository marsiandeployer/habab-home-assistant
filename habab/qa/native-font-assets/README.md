# Native font fix acceptance

Source commit: 51e42204781d6076b38bcb27b3918b2d0910d6a3.
The new immutable frontend wheel is 20260930.4+habab.4. The previous .3 wheel
and the stable Windows 0.3.1 archive were preserved.

The full pinned frontend build passed lint, Prettier, 10 focused tests, both
typechecks, one Rspack build, compression and wheel packaging. The first
attempt failed because generated icon/translation metadata did not exist yet.
The builder order was corrected and the remaining stages completed once in
the same prepared worktree. No permanent skip-check or resume mode was added.

The new wheel contains all 12 Roboto WOFF2 assets. Regular, Medium and Bold
have the correct WOFF2 signature and match the gathered source bytes and SHA.
The same guard rejects the old wheel and accepts a valid source directory
without dots in its parent path. Root verified the public wheel HTTP body SHA.
See `build-summary.json`, `font-assets-report.json` and `root-wheel-readback.json`.

The allowlisted Windows ZIP was generated from a clean Git archive. Root
compared all 76 members against tracked source, allowing only documented BAT
CRLF conversion. A Linux image was built from the extracted ZIP. All 50 baked
assistant and Telegram source files match the ZIP. Those backend files have
no diff from the earlier source which passed 141 Python and 12 Telegram tests;
those suites were not repeated for this frontend asset change.

Root started only its isolated Core on localhost8403 with no source overlays.
The actual Core served the three required font files with HTTP 200 and exact
expected SHA/size. Browser font loading returned loaded Roboto faces for
weights 400, 500 and 700. The shared UI harness passed seven desktop/mobile
cases with no JavaScript errors or failed requests, preserving the draft and
existing browser tabs. All screenshots were opened and checked. The first
font-only screenshot caught the launch screen before its removal; it is kept
as `initial-font-ready-loading.png`. The corrected capture explicitly waits
for the launch screen to disappear.

The real native Local To-do form also loaded from this wheel. Focusing its
input expanded the mobile chat to844px and made the dashboard inert; collapse
restored422px. The unsubmitted test flow was cancelled. The own test container
was stopped afterwards and its configuration preserved. The native-form
runner reused `../native-form-mobile/check-native-ha.cjs` with only the accepted
source identity, output directory and no-overlay metadata adjusted in a
private one-off driver.

The candidate ZIP is published at
https://habab-demo.wpmix.net/downloads/habab-home-windows-604e4f9a793b.zip
with SHA-256 604e4f9a793bf73010a418585ad44d7a7df10536183c9be288eebfbd6d6e8b0c.
`publication.json` records full HTTP readback. Extract it and run
`start-windows.bat`; the separate stable bootstrap launchers remain unchanged.

This is Linux image/browser acceptance of the Windows payload. Physical
Windows installation, a physical Android keyboard, and Xiaomi connection on
the user's Windows were not tested. The selected Windows Chrome tunnel did
not connect; no user settings or device commands were changed. The installed
Android runtime was not updated by this work.
