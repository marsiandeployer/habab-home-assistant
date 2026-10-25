# Issue #10: source-only branding review

**Finding:** the public demo release still advertises the upstream demo name in its browser tab and installed PWA metadata.

The local public entrypoint `habab/public/index.html` has `<title>Home Assistant Demo</title>` and links to its versioned release manifest. That manifest (`habab/public/releases/20261021-ui5/manifest.json`) declares `name: Home Assistant Demo` and `short_name: HA Demo`.

The persistent source of the generated entrypoint and manifest is `habab/build/publish_build.py`. `scope_html()` carries the upstream title through unchanged. Its manifest transform rewrites only the subpath `start_url` and icon paths, leaving `name`, `short_name`, and the generic Home Assistant description intact. Therefore editing a generated release alone would not survive the next static republish.

**Source fix applied:** commit `60f7cef1d` changes the generated document title and staged manifest `name`/`short_name` to `Habab Home`. The description, frontend wheel, and upstream attribution were left unchanged. The change affects the public demo browser title and PWA install label; generated release files still need a separate build and live check.

Attribution is separate and remains intact: the publisher writes `ATTRIBUTION.md` for the official upstream commit/license, and the current launch entrypoint includes the Open Home Foundation lockup. Neither needs removal or modification for this gap.

Other inspected target: Android's `strings.xml` calls the app `Habab Home`; `AndroidManifest.xml` uses that resource as the app label, and `MainActivity` reuses it as the shell heading. The embedded login loads the Home Assistant page inside a WebView. This review recommends the public title/PWA gap above as the one minimal target; it makes no claim that the native shell/login branding is complete.

An isolated temporary fixture verified the rendered title, both manifest names, the existing start URL transform, and unchanged Open Home Foundation lockup/upstream attribution. No test suite, frontend wheel, real static release, browser, tunnel, Core, or Android device was run.
