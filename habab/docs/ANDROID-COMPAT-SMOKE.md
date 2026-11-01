# Android compatibility smoke

This manual workflow uses `ubuntu-24.04` standard hosted
runners in the public repository. It checks the unchanged public ARM64 APK on
Google APIs x86_64 API 35 and 36 images, one job at a time.
The first real run is pending; local runner contracts have passed.

The workflow, runner and offline tests are integrated separately from the
product runtime and release pipeline. The workflow has only
`workflow_dispatch`; there are no push, PR, schedule, publish or deploy triggers.

The operator supplies the immutable public `release_tag`, reviewed `apk_sha256`
and exact `apk_bytes` for `habab-home-arm64.apk`. The script checks public release
metadata, the expected asset URL, downloaded bytes and SHA-256. It does not use
a GitHub API token or any provider credential. A missing or mismatched artifact
fails. Use only a public release; do not upload the private APK/model/audio pack.

The job uses preinstalled SDK command-line tools, ADB and emulator. It accepts
SDK licenses and installs the selected official system image on the disposable
runner. Missing KVM, inaccessible KVM, failed acceleration, missing SDK tools or
an image without `arm64-v8a` and an active native bridge are explicit failures
with `INFRA_UNAVAILABLE`. They are never skipped or converted into a green test.
The runner verifies the installed image's API, ABI, Google APIs tag and vendor,
then checks the booted guest's API. The report records emulator version, image
revision, guest ABIs/native bridge, page size and installed APK version.
Current image revisions have not yet been capability-probed.

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
are not uploaded.

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
