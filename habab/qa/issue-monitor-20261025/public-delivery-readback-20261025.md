# Public delivery readback

## Windows preview

Source `48c2c65586cf152ebb2f28a4287291e58e89a3b4` produced Windows preview `0.3.6`, ZIP `habab-home-windows-59d23122eb97.zip`. The public ZIP, BAT, and preview metadata each returned HTTP 200 and matched their recorded SHA-256; the ZIP CRC and source-member checks passed. See the [publication receipt](windows-preview-publication-40229596073.json). This is a preview, not the stable Windows package or a physical installation.

## Android runtime and APK preview

Runtime `release-3149ea67a5e9691e` from source `3485669f466d7c5337fb3b7866fac737f9d871c3` is published. The runtime manifest and signature matched exact public GET readbacks. The public archive was checked by HEAD and a 64 KiB range; the full archive SHA-256 was verified against the local canonical copy, but the full archive was not downloaded from the public endpoint. Retention points to runtime `3149ea67a5e9691e` as current and `2f94e2e4d62bf401` as previous.

APK preview 19 (`0.1.11-preview`, identifier `5b3946fadbda`) is 51,421,091 bytes; its full public GET SHA-256 matched the canonical build copy. The build receipt records external version/signing overrides because source defaults still say version 18; its debug signer matches APK18. APK retention is current 19 / previous 18, with APK17 pruned. The APK is not installed and has not been promoted to stable. Android signing/build in CI and physical acceptance remain **NOT RUN**.

## Public demo chat and title

The public demo chat fix has a three-module closure; each module returned HTTP 200, with zero JavaScript 404s and one benign anonymous reply. No prompt or reply text is retained here. This bounded chat readback does not establish voice or physical-device acceptance.

Source commit `e5babaf2ad473dd0282b959a4ef2863aacdbf3fb` scopes the page-title brand to the demo marker. A targeted compile and public runtime-title readback are still pending; do not count the source change as published title acceptance.
