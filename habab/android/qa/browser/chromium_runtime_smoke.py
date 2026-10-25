#!/usr/bin/env python3
"""Run a bounded, unauthenticated Chromium smoke test in Android PRoot.

Guest mode runs from the existing OpenCode shell. To use it, the operator must
explicitly place this source file at /config/chromium_runtime_smoke.py first;
the APK does not contain this QA tool. The host/ADB mode requires a debuggable
APK; published preview APK19 is not debuggable. Guest mode prints a sanitized
JSON receipt without requiring run-as.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile

TOOLS = Path(__file__).resolve().parents[2] / "tools"

PACKAGE = "ru.habab.home"
GUEST_BROWSER = "/usr/local/bin/chromium"
OFFICIAL_PAGE = "https://developer.chrome.com/docs/automation-and-testing/headless-cli/"
LOCAL_JS_RESULT_MARKUP = b'<body data-smoke="HABAB_LOCAL_JS_OK">'


def local_dom_has_js_result(serialized_dom):
    return LOCAL_JS_RESULT_MARKUP in serialized_dom


class GuestCancelled(Exception):
    def __init__(self, signal_number):
        self.signal_number = signal_number


REMOTE_SCRIPT = r'''#!/system/bin/sh
set -u
native_dir=${NATIVE_DIR:-}
app_data=$(pwd)
files="$app_data/files"
runtime="$files/runtime"
tmpbase="$files/tmp"
run_id="chromium-smoke-$$"
test_dir="$tmpbase/$run_id"
proot="$native_dir/libproot.so"
receipt="$test_dir/receipt.json"
watchdog_pid=
proot_pid=

emit_failure() {
    stage=$1
    code=$2
    printf '{"kind":"android-chromium-runtime-smoke","status":"FAIL","stage":"%s","terminal":"EXIT","exitCode":%s}\n' "$stage" "$code"
}
cleanup() {
    if [ -n "$proot_pid" ]; then
        kill -TERM "$proot_pid" 2>/dev/null || :
        wait "$proot_pid" 2>/dev/null || :
    fi
    if [ -n "$watchdog_pid" ]; then
        kill "$watchdog_pid" 2>/dev/null || :
        wait "$watchdog_pid" 2>/dev/null || :
    fi
    [ ! -d "$test_dir" ] || rm -rf -- "$test_dir"
}

if [ -z "$native_dir" ] || [ ! -x "$proot" ] || [ ! -r "$native_dir/libproot-loader.so" ]; then
    emit_failure native_proot 2
    exit 2
fi
if [ ! -f "$runtime/etc/habab-android-release.json" ] || \
   [ ! -f "$runtime/etc/habab-android-network-tools.json" ] || \
   [ ! -x "$runtime/usr/local/bin/chromium" ] || \
   ! grep -Fq '"chromiumVersion": "155.0.8059.84-r0"' "$runtime/etc/habab-android-network-tools.json" || \
   [ ! -f "$files/android-network.json" ] || [ ! -d "$tmpbase" ]; then
    emit_failure runtime_or_network_preflight 3
    exit 3
fi
umask 077
if ! mkdir "$test_dir" 2>/dev/null; then
    emit_failure private_temp_directory 4
    exit 4
fi
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

cat > "$test_dir/guest-test.sh" <<'GUEST'
#!/bin/sh
set -eu
umask 077
mkdir -m 700 /qa-smoke/profiles || exit 6
export TMPDIR=/qa-smoke/profiles
version=$(/usr/local/bin/chromium --version 2>/dev/null) || exit 31
printf '%s\n' "$version" | grep -Eq '^Chromium [0-9][0-9.]*$' || exit 32
fail() {
    printf '{"kind":"android-chromium-runtime-smoke","status":"FAIL","stage":"%s","terminal":"EXIT","browserVersion":"%s","timeoutSecondsPerPage":25,"exitCode":%s}\n' "$1" "$version" "$2" > /qa-smoke/receipt.json
    exit "$2"
}
cat > /qa-smoke/local.html <<'HTML'
<!doctype html><html><head><title>Habab local browser check</title></head>
<body><p>local test</p><script>document.body.dataset.smoke = 'HABAB_LOCAL_JS_OK';</script></body></html>
HTML
if timeout -k 3 25 /usr/local/bin/chromium --dump-dom --timeout=15000 file:///qa-smoke/local.html > /qa-smoke/local.dom 2>/dev/null; then
    grep -Fq '<body data-smoke="HABAB_LOCAL_JS_OK">' /qa-smoke/local.dom || fail local_file_html 41
else
    result=$?
    fail local_file_html "$result"
fi
if timeout -k 3 25 /usr/local/bin/chromium --dump-dom --timeout=15000 \
    https://developer.chrome.com/docs/automation-and-testing/headless-cli/ \
    > /qa-smoke/https.dom 2>/dev/null; then
    grep -Fq -- '--dump-dom' /qa-smoke/https.dom || fail official_https_page 42
else
    result=$?
    fail official_https_page "$result"
fi
printf '{"kind":"android-chromium-runtime-smoke","status":"PASS","stage":"complete","terminal":"EXIT","browserVersion":"%s","localFileHtmlAndJs":"PASS","officialHttpsPage":"PASS","timeoutSecondsPerPage":25,"prootTimeoutSeconds":75,"exitCode":0}\n' "$version" > /qa-smoke/receipt.json
GUEST

set --
for system_path in /sys /system /vendor /apex; do
    if [ -e "$system_path" ]; then set -- "$@" -b "$system_path"; fi
done
export LD_LIBRARY_PATH="$native_dir"
export PROOT_LOADER="$native_dir/libproot-loader.so"
export PROOT_TMP_DIR="$tmpbase"
export PROOT_NO_SECCOMP=1
export HOME=/root TMPDIR=/tmp
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LANG=C.UTF-8 TZ=Europe/Moscow SSL_CERT_FILE=/etc/ssl/cert.pem

"$proot" --kill-on-exit -0 -r "$runtime" -b /dev -b /proc \
    -b "$tmpbase:/tmp" -b "$files/config:/config" \
    -b "$files/android-network.json:/run/habab-network.json" "$@" \
    -b "$test_dir:/qa-smoke" -w /config /bin/sh /qa-smoke/guest-test.sh \
    > "$test_dir/proot.log" 2>&1 &
proot_pid=$!
(
    sleep 75
    : > "$test_dir/watchdog-fired"
    kill -TERM "$proot_pid" 2>/dev/null || :
) &
watchdog_pid=$!
wait "$proot_pid"
exit_code=$?
proot_pid=
kill "$watchdog_pid" 2>/dev/null || :
wait "$watchdog_pid" 2>/dev/null || :
watchdog_pid=
if [ -f "$test_dir/watchdog-fired" ]; then
    printf '{"kind":"android-chromium-runtime-smoke","status":"TIMEOUT","stage":"proot","terminal":"TIMEOUT","timeoutSecondsPerPage":25,"prootTimeoutSeconds":75,"exitCode":%s}\n' "$exit_code"
elif [ -f "$receipt" ]; then
    cat "$receipt"
else
    emit_failure proot "$exit_code"
fi
exit "$exit_code"
'''


def print_receipt(**fields):
    print(json.dumps({"kind": "android-chromium-runtime-smoke", **fields}, separators=(",", ":")))


def stop_process_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_guest_browser(arguments, output, environment, timeout_seconds):
    with output.open("wb") as stdout:
        process = subprocess.Popen(
            [GUEST_BROWSER, *arguments], stdout=stdout, stderr=subprocess.DEVNULL,
            env=environment, start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout_seconds), False
        except subprocess.TimeoutExpired:
            stop_process_group(process)
            return 124, True
        except BaseException:
            stop_process_group(process)
            raise


def guest_smoke():
    receipt = {"mode": "guest", "terminal": "EXIT", "timeoutSecondsPerPage": 25,
               "localFileHtmlAndJs": "NOT_RUN", "officialHttpsPage": "NOT_RUN"}
    version = None
    try:
        with tempfile.TemporaryDirectory(prefix="habab-chromium-smoke-") as directory:
            work = Path(directory)
            profiles = work / "profiles"
            profiles.mkdir(mode=0o700)
            environment = os.environ.copy()
            environment["TMPDIR"] = str(profiles)
            environment["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
            environment["LANG"] = "C.UTF-8"

            version_file = work / "version.txt"
            code, timed_out = run_guest_browser(["--version"], version_file, environment, 10)
            if timed_out:
                receipt.update(status="TIMEOUT", stage="version", terminal="TIMEOUT", exitCode=124)
                print_receipt(**receipt)
                return 1
            if code != 0 or version_file.stat().st_size > 256:
                receipt.update(status="FAIL", stage="version", exitCode=code or 65)
                print_receipt(**receipt)
                return 1
            version = version_file.read_text(encoding="utf-8", errors="replace").strip()
            if not re.fullmatch(r"Chromium [0-9]+(?:\.[0-9]+)+", version):
                receipt.update(status="FAIL", stage="version", exitCode=65)
                print_receipt(**receipt)
                return 1
            receipt["browserVersion"] = version

            local_html = work / "local.html"
            local_html.write_text(
                "<!doctype html><html><body><p>local test</p>"
                "<script>document.body.dataset.smoke='HABAB_LOCAL_JS_OK';</script>"
                "</body></html>", encoding="utf-8",
            )
            pages = [
                ("localFileHtmlAndJs", ["--dump-dom", "--timeout=15000", local_html.as_uri()],
                 LOCAL_JS_RESULT_MARKUP),
                ("officialHttpsPage", ["--dump-dom", "--timeout=15000", OFFICIAL_PAGE], b"--dump-dom"),
            ]
            for field, arguments, marker in pages:
                output = work / (field + ".dom")
                code, timed_out = run_guest_browser(arguments, output, environment, 25)
                if timed_out:
                    receipt.update(status="TIMEOUT", stage=field, terminal="TIMEOUT", exitCode=124)
                    print_receipt(**receipt)
                    return 1
                if code != 0 or output.stat().st_size > 8 * 1024 * 1024:
                    receipt.update(status="FAIL", stage=field, exitCode=code or 65)
                    print_receipt(**receipt)
                    return 1
                serialized_dom = output.read_bytes()
                if (not local_dom_has_js_result(serialized_dom) if field == "localFileHtmlAndJs"
                        else marker not in serialized_dom):
                    receipt.update(status="FAIL", stage=field, exitCode=42)
                    print_receipt(**receipt)
                    return 1
                receipt[field] = "PASS"

        receipt.update(status="PASS", stage="complete", exitCode=0)
        print_receipt(**receipt)
        return 0
    except GuestCancelled as error:
        receipt.update(status="CANCELLED", stage="signal", terminal="EXIT",
                       exitCode=128 + error.signal_number)
        print_receipt(**receipt)
        return 1
    except (OSError, subprocess.SubprocessError):
        receipt.update(status="FAIL", stage="browser_process", exitCode=127)
        if version:
            receipt["browserVersion"] = version
        print_receipt(**receipt)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest", action="store_true",
                        help="run directly inside the existing Android PRoot shell, without ADB")
    parser.add_argument("--adb", default=os.environ.get("HABAB_ADB", "adb"))
    parser.add_argument("--serial", help="Select the single attached Huawei P30 Pro by serial")
    args = parser.parse_args()

    if args.guest:
        def cancel_guest(signum, _frame):
            raise GuestCancelled(signum)
        signal.signal(signal.SIGINT, cancel_guest)
        signal.signal(signal.SIGTERM, cancel_guest)
        return guest_smoke()

    sys.path.insert(0, str(TOOLS))
    from device import select_device

    try:
        serial = select_device(args.serial, [args.adb])
    except Exception:
        print_receipt(status="BLOCKED", stage="device_selection", terminal="EXIT", exitCode=None)
        return 2

    adb = [args.adb, "-s", serial, "shell"]
    try:
        paths = subprocess.run(adb + ["pm", "path", PACKAGE], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        print_receipt(status="BLOCKED", stage="package_path_lookup", terminal="TIMEOUT", exitCode=None)
        return 2
    base_apks = [line.removeprefix("package:").strip() for line in paths.stdout.splitlines()
                 if line.startswith("package:") and line.rstrip().endswith("/base.apk")]
    if paths.returncode or len(base_apks) != 1:
        print_receipt(status="BLOCKED", stage="base_apk_lookup", terminal="EXIT", exitCode=paths.returncode)
        return 2

    native_dir = str(Path(base_apks[0]).parent / "lib" / "arm64")
    command = ("run-as " + PACKAGE + " sh -c "
               + "'NATIVE_DIR=\"$1\"; export NATIVE_DIR; exec sh -s' sh "
               + shlex.quote(native_dir))
    try:
        result = subprocess.run(adb + [command], input=REMOTE_SCRIPT, capture_output=True,
                                text=True, timeout=95)
    except (OSError, subprocess.TimeoutExpired):
        print_receipt(status="TIMEOUT", stage="adb_runtime_smoke", terminal="TIMEOUT",
                      timeoutSeconds=95, exitCode=None)
        return 1

    lines = [line for line in result.stdout.splitlines() if line.startswith('{') and line.endswith('}')]
    if lines:
        try:
            receipt = json.loads(lines[-1])
            allowed = {"kind", "status", "stage", "terminal", "browserVersion", "localFileHtmlAndJs",
                       "officialHttpsPage", "timeoutSecondsPerPage", "prootTimeoutSeconds", "exitCode"}
            receipt = {key: value for key, value in receipt.items() if key in allowed}
            if receipt.get("status") == "PASS" and result.returncode == 0:
                print(json.dumps(receipt, separators=(",", ":")))
                return 0
            print(json.dumps(receipt, separators=(",", ":")))
            return 1
        except (json.JSONDecodeError, TypeError):
            pass
    print_receipt(status="BLOCKED", stage="run_as_or_receipt", terminal="EXIT", exitCode=result.returncode)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
