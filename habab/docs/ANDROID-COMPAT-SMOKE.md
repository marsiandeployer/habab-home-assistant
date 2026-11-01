# Android compatibility smoke

This manual workflow uses `ubuntu-24.04` standard hosted
runners in the public repository. It checks the unchanged public ARM64 APK on
Google APIs x86_64 API 35 and 36 images, one job at a time.
The accepted [run 41293944520](https://github.com/marsiandeployer/habab-home-assistant/actions/runs/41293944520)
completed on runner source `6c4609532f9743b24447cd24e3a9f778bd40ecee` with the
unchanged public APK59 (`0.1.59-usb-zigbee`), release
`habab-v0.3.6-6a15321f46e3`, SHA-256
`d5e4b983eab6c98acbc171ea136f795638632436b82c24414e52dde40ed0c9af`,
60187091 bytes. This workflow does not rebuild or republish the APK.

| Guest API | Google APIs x86_64 image revision | Install and MainActivity | Emulator cleanup |
| --- | --- | --- | --- |
| 35 | 9 | PASS | PASS |
| 36 | 7 | PASS | PASS |

Both guests reported `x86_64,arm64-v8a`, `libndk_translation.so`, native bridge
exec enabled and 4096-byte pages. Both screenshots were opened and visually
inspected: the Habab Home native screen was visible, with the server runtime
uninstalled/stopped as expected for this offline test. No fatal exception or
signal was captured; runtime and voice services were absent.

The emulator and avdmanager share one explicit owned `ANDROID_AVD_HOME` under
the job's temporary output directory. Ubuntu's `libpulse0` supplies the host
library required by the emulator; guest audio remains disabled.

The workflow, runner and offline tests are integrated separately from the
product runtime and release pipeline. The workflow has only
`workflow_dispatch`; there are no push, PR, schedule, publish or deploy triggers.

The operator supplies the immutable public `release_tag`, reviewed `apk_sha256`
and exact `apk_bytes` for `habab-home-arm64.apk`. The script checks public release
metadata, the expected asset URL, downloaded bytes and SHA-256. It does not use
a GitHub API token or any provider credential. A missing or mismatched artifact
fails. Use only a public release; do not upload the private APK/model/audio pack.
HTTP 429 and 5xx receive one explicit retry; other HTTP errors fail immediately
with their numeric status. No credential or alternate download source is used.

The job uses preinstalled SDK command-line tools and ADB. It accepts
SDK licenses and installs the official emulator and selected system image on the disposable
runner. Missing KVM, inaccessible KVM, failed acceleration, missing SDK tools or
an image without `arm64-v8a` and an active native bridge are explicit failures
with `INFRA_UNAVAILABLE`. They are never skipped or converted into a green test.
The runner installs the exact Google SDK package ID and checks the booted
guest's API and ARM64 translation capability. Image properties are diagnostic;
missing optional vendor fields do not block the app test. The report records emulator version, image
revision, guest ABIs/native bridge, page size and installed APK version.
Before downloading the large system image, the runner checks both installed
emulator/QEMU binaries with `ldd` and verifies KVM with `-accel-check`. Direct
QEMU `ldd` can list bundled libraries supplied by its launcher; those entries
are diagnostic, while the actual launcher/KVM check must succeed.

Before app launch the guest's Wi-Fi and mobile data are disabled, and the job
requires `Active default network: none/null`. This intentionally offline smoke
cannot download the factory runtime. An unfamiliar connectivity dump format
fails closed before app launch. The app is started with an explicit MainActivity
intent without `CATEGORY_LAUNCHER`, so the existing launcher's automatic runtime
provisioning is not requested. The emulator uses `-no-audio`; no microphone
permission is granted and no voice, model or device request is sent. Unexpected
server, maintenance or voice services cause failure.

PASS means installation succeeded, MainActivity remained resumed/alive after
five seconds, no app `FATAL EXCEPTION`/fatal signal was captured, and runtime/
voice services were absent. Uploaded evidence is a report, a PNG screenshot and
bounded app-PID logcat with credential/URL redaction. Screenshots still require
human visual inspection after the first real run. The exact owned emulator is
stopped in cleanup, including its owned QEMU process group if the launcher exits
first. Existing APK files in the output directory are preserved and cause an
input failure. Evidence retention is one day; APK and raw emulator output
are not uploaded. Failures retain the tool name, safe subcommand, timeout or
exit status, and a redacted emulator stderr tail (60 lines, at most 6000 chars).
An exited emulator fails the boot loop immediately.

The workflow runs offline runner contracts before downloading SDK images. Run
them locally without ADB, an emulator or network access:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=habab/android/tools python3 -m unittest -v test_compat_smoke
```

This does not prove PRoot execution, ARM child processes/ptrace, Core startup,
provider requests, JNI loading or inference, 16 KB native compatibility,
microphone/speaker behavior, battery/background behavior, or physical wake-word
quality. A Java UI surviving ARM translation is not full native acceptance.
The existing P30 remains the physical acceptance device. Separate native tests
must exercise the real packaged libraries and actual PRoot paths before making
those claims. `targetSdk 28` running on a modern OS is also not target-35/36
policy compliance.

Official references:

- [GitHub hosted runners and Android acceleration](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Android ARM app translation](https://android-developers.googleblog.com/2020/03/run-arm-apps-on-android-emulator.html)
- [AOSP Native Bridge scope](https://android.googlesource.com/platform/art/+/refs/heads/main/libnativebridge/README.md)
- [Android emulator command line](https://developer.android.com/studio/run/emulator-commandline)
- [Android 16 KB compatibility](https://developer.android.com/guide/practices/page-sizes)
