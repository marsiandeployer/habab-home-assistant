# Windows candidate from source 0f340bd88

This is a private candidate build, not a stable Windows release. The stable
public archive and launchers were not changed.

The clean Git export and its allowlisted ZIP were built into separate Linux
images. Root independently compared every one of the ZIP's 76 members against
commit 0f340bd88, allowing only the builder's documented BAT CRLF conversion.
The ZIP has SHA-256 cb1ffc67434c8c8f0b038ada0c42f3a51f2fe0e4db1c7f68306ee89222405223.

The image built from the extracted ZIP passed 141 Python tests, 9 chat lifecycle
tests, and 12 Telegram transport tests with networking disabled. npm is absent
from the image, so the transport package's `node --test` script was invoked
directly. These tests made no provider calls and sent no Telegram messages.

Root started only its isolated Core on localhost8403, preserving its test
configuration and mounting no source overlays. The existing shared assistant
browser harness passed seven desktop/mobile cases with no JavaScript errors.
All four screenshots were opened and inspected. Mobile keyboard space was
emulated by resizing the viewport. No physical Windows installation was tested.
The test container was stopped afterwards; its configuration was preserved.

Runtime acceptance remains PARTIAL: the browser reported aborted font loads,
and direct HTTP probes returned 404 for Roboto Regular, Medium, and Bold.
The installed frontend wheel 20260930.4+habab.3 contains no WOFF files. The UI
renders with fallback fonts. This finding prevents promotion of this candidate
while the versioned frontend asset fix is prepared; it does not erase the
separately recorded chat behavior results.

Reports:
- `candidate-package.json`: generated ZIP member manifest and credential scan.
- `root-package-readback.json`: independent comparison with tracked Git source.
- `build.json`: image identities, baked metadata, and network-disabled tests.
- `ui/private-ui.json`: actual baked Core browser behavior and font failures.
- `runtime-readback.json`: baked UI hash, no-overlay check, and direct font probes.

No user Windows settings, Xiaomi integration, or physical device were changed.
