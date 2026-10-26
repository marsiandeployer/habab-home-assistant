#!/usr/bin/env python3
"""Bounded P30 smoke for the packaged browser tool and pinned OpenCode loader.

Runs only against a local HTTP fixture. It makes no model or public-site request,
uses a temporary OpenCode HOME, and persists only a sanitized receipt on the host.
Review the script before invoking it with --serial.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[4]
PACKAGE = "ru.habab.home"
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
TOOL_FILES = ("habab_browser.js", "browser-control.mjs")
REMOTE_STAGES = frozenset({
    "remote_started", "native_runtime_ok", "runtime_binary_ok", "temp_created",
    "isolation_home_ready", "source_staging", "tool_sources_staged", "fixture_script_staged",
    "proot_starting", "proot_invoked", "proot_finished", "guest_started", "fixture_starting", "fixture_started",
    "manifest_checking", "manifest_verified", "opencode_invoking", "opencode_returned",
    "receipt_written", "proot_finished", "cleanup_started",
})
HOST_STAGES = frozenset({
    "host_started", "package_path_lookup_started", "package_path_lookup_finished",
    "package_path_validated", "adb_runtime_started", "adb_runtime_finished",
})

PROOT_ERROR_PATTERNS = {
    "BIND_OR_MOUNT": r"(bind|mount).*(failed|error|no such file|permission denied)|(failed|error).*(bind|mount)",
    "GUEST_EXEC": r"(execve|cannot execute|cannot open|no such file or directory|not found)",
}
PROOT_ERROR_CLASSES = frozenset((*PROOT_ERROR_PATTERNS, "EMPTY", "OTHER"))


def classify_proot_error(output):
    if not output.strip():
        return "EMPTY"
    for label, pattern in PROOT_ERROR_PATTERNS.items():
        if re.search(pattern, output, re.IGNORECASE):
            return label
    return "OTHER"


def check_proot_invocation(remote):
    expected = ('/system/bin/toybox timeout -k 3 72 "$native_dir/libproot.so" --kill-on-exit -0 -r "$runtime" '
                '-b /dev -b /proc -b "$test_dir/tmp:/tmp" -b "$test_dir:/qa" '
                '-w /qa/workspace /bin/sh /qa/guest-test.sh >"$test_dir/proot.log" 2>&1')
    launch_lines = [line for line in remote.splitlines() if line.startswith('/system/bin/toybox timeout -k 3 72 ')]
    expected_env = ('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin '
                    'LANG=C.UTF-8 TZ=Europe/Moscow SSL_CERT_FILE=/etc/ssl/cert.pem HOME=/root TMPDIR=/tmp')
    return (len(launch_lines) == 1 and launch_lines[0] == expected
            and expected_env in remote
            and "system_path in /sys /system /vendor /apex" not in remote
            and "watchdog_pid" not in remote and "sleep 72" not in remote)


def check_proot_error_classification():
    cases = {
        "": "EMPTY",
        "proot error: bind failed": "BIND_OR_MOUNT",
        "execve('/bin/sh') failed: No such file or directory": "GUEST_EXEC",
        "diagnostic details omitted": "OTHER",
    }
    return all(classify_proot_error(text) == expected for text, expected in cases.items())


def check_foreground_timeout_runner():
    timeout = shutil.which("timeout")
    if not timeout:
        return False
    started = time.monotonic()
    try:
        result = subprocess.run([timeout, "-k", "1", "2", sys.executable, "-B", "-c", "pass"],
                                capture_output=True, timeout=4)
    except subprocess.TimeoutExpired:
        return False
    return (result.returncode == 0 and not result.stdout and not result.stderr
            and time.monotonic() - started < 1.5)


def fixture_readiness_probe(url, deadline_seconds=6):
    """Return a Python probe that only requests the supplied loopback fixture URL."""
    return f'''import socket, time
from urllib import error, request
url = {url!r}
deadline = time.monotonic() + {deadline_seconds}
while True:
    try:
        with request.urlopen(url, timeout=1) as response:
            body = response.read(512)
            if response.status != 200:
                raise SystemExit(42)
            if b"Issue21 local fixture" not in body:
                raise SystemExit(43)
            raise SystemExit(0)
    except error.HTTPError:
        raise SystemExit(42)
    except error.URLError as exc:
        reason = exc.reason
        if not isinstance(reason, (ConnectionRefusedError, TimeoutError, socket.timeout)):
            raise SystemExit(45)
    except (TimeoutError, socket.timeout):
        pass
    if time.monotonic() >= deadline:
        raise SystemExit(44)
    time.sleep(0.2)
'''


def check_fixture_readiness_probe():
    """Exercise readiness and timeout classification against a local-only server."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"Issue21 local fixture"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/fixture"
    try:
        ready = subprocess.run([sys.executable, "-B", "-c", fixture_readiness_probe(url, 1)],
                               capture_output=True, timeout=4)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    if ready.returncode != 0 or ready.stdout or ready.stderr:
        return False
    unavailable = subprocess.run([sys.executable, "-B", "-c", fixture_readiness_probe(url, 1)],
                                 capture_output=True, timeout=4)
    return unavailable.returncode == 44 and not unavailable.stdout and not unavailable.stderr


REMOTE_TEMPLATE = r'''#!/system/bin/sh
set -u
native_dir=${NATIVE_DIR:-}
files=$(pwd)/files
runtime="$files/runtime"
tmpbase="$files/tmp"
test_dir="$tmpbase/issue21-browser-tool-$$"
receipt="$test_dir/receipt.json"
stage_file="$test_dir/stage-markers.txt"
write_stage() {
    case "$1" in
        remote_started|native_runtime_ok|runtime_binary_ok|temp_created|isolation_home_ready|source_staging|tool_sources_staged|fixture_script_staged|proot_starting|proot_invoked|proot_finished|guest_started|fixture_starting|fixture_started|manifest_checking|manifest_verified|opencode_invoking|opencode_returned|receipt_written|cleanup_started) printf '%s\n' "$1" >> "$stage_file" ;;
        *) return 1 ;;
    esac
}
stage_list() { [ ! -f "$stage_file" ] || tr '\n' ',' < "$stage_file" | tr -cd 'a-z0-9_,-'; }
cleanup() {
    [ ! -d "$test_dir" ] || rm -rf -- "$test_dir"
}
emit() { printf '{"kind":"issue21_pinned_browser_tool","status":"%s","stage":"%s","exitCode":%s,"stageMarkers":"%s"}\n' "$1" "$2" "$3" "$(stage_list)"; }
emit_proot() { printf '{"kind":"issue21_pinned_browser_tool","status":"%s","stage":"%s","exitCode":%s,"prootErrorClass":"%s","stageMarkers":"%s"}\n' "$1" "$2" "$3" "$4" "$(stage_list)"; }
fail() { printf '{"kind":"issue21_pinned_browser_tool","status":"FAIL","stage":"%s","exitCode":%s,"stageMarkers":"%s"}\n' "$1" "$2" "$(stage_list)" > "$receipt"; exit "$2"; }
if [ -z "$native_dir" ] || [ ! -x "$native_dir/libproot.so" ] || [ ! -r "$native_dir/libproot-loader.so" ]; then emit FAIL native_runtime 20; exit 20; fi
if [ ! -d "$tmpbase" ] || [ -e "$test_dir" ]; then emit FAIL temp_preflight 22; exit 22; fi
umask 077
mkdir -m 700 "$test_dir" || { emit FAIL temp_create 23; exit 23; }
write_stage remote_started
write_stage native_runtime_ok
write_stage temp_created
if [ ! -x "$runtime/opt/habab-opencode/opencode" ] || [ ! -x "$runtime/usr/local/bin/chromium" ]; then emit FAIL runtime_binary 21; exit 21; fi
write_stage runtime_binary_ok
trap cleanup EXIT HUP INT TERM
mkdir -m 700 -p "$test_dir/home/.opencode/tools" "$test_dir/config" "$test_dir/data" "$test_dir/state" "$test_dir/cache" "$test_dir/workspace" "$test_dir/tmp" || fail temp_layout 24
write_stage isolation_home_ready
write_stage source_staging
printf '%s' '__CONTROL_B64__' | base64 -d > "$test_dir/home/.opencode/tools/browser-control.mjs" || fail source_stage 25
printf '%s' '__TOOL_B64__' | base64 -d > "$test_dir/home/.opencode/tools/habab_browser.js" || fail source_stage 25
write_stage tool_sources_staged
cat > "$test_dir/fixture_server.py" <<'PYFIXTURE'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'<!doctype html><html><head><title>Issue21 local fixture</title></head><body><input id="query"><button id="go">Go</button><p id="result" data-state="waiting">waiting</p><script>document.querySelector("#go").addEventListener("click",()=>{document.querySelector("#result").textContent="done:"+document.querySelector("#query").value;document.querySelector("#result").dataset.state="done"})</script></body></html>'
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
try:
    server=ThreadingHTTPServer(('127.0.0.1',18765),Handler)
except OSError:
    print('FIXTURE_BIND_ERROR')
    raise
server.serve_forever()
PYFIXTURE
cat > "$test_dir/guest-test.sh" <<'GUEST'
#!/bin/sh
set -eu
umask 077
write_stage() {
    case "$1" in
        guest_started|fixture_starting|fixture_started|manifest_checking|manifest_verified|opencode_invoking|opencode_returned|receipt_written) printf '%s\n' "$1" >> /qa/stage-markers.txt ;;
        *) return 1 ;;
    esac
}
write_stage guest_started
write_stage fixture_starting
python3 /qa/fixture_server.py >/qa/server.out 2>&1 &
fixture_pid=$!
cleanup_guest() { kill "$fixture_pid" 2>/dev/null || :; wait "$fixture_pid" 2>/dev/null || :; }
trap cleanup_guest EXIT HUP INT TERM
set +e
timeout -k 2 8 python3 - <<'READINESS'
__FIXTURE_READINESS_PROBE__
READINESS
readiness_code=$?
set -e
if [ "$readiness_code" -ne 0 ]; then
    if grep -Fq 'FIXTURE_BIND_ERROR' /qa/server.out 2>/dev/null; then fail fixture_bind_error 31
    elif grep -Eq 'ModuleNotFoundError|ImportError' /qa/server.out 2>/dev/null; then fail fixture_import_error 32
    elif grep -Eq 'PermissionError|Permission denied' /qa/server.out 2>/dev/null; then fail fixture_permission_error 33
    elif [ "$readiness_code" -eq 42 ]; then fail fixture_http_status 34
    elif [ "$readiness_code" -eq 43 ]; then fail fixture_body_mismatch 35
    elif [ "$readiness_code" -eq 45 ]; then fail fixture_connect_error 36
    elif [ "$readiness_code" -eq 124 ]; then fail fixture_probe_timeout 37
    elif [ "$readiness_code" -eq 127 ]; then fail fixture_probe_unavailable 39
    else fail fixture_readiness_timeout 38
    fi
fi
write_stage fixture_started
export HOME=/qa/home XDG_CONFIG_HOME=/qa/config XDG_DATA_HOME=/qa/data XDG_STATE_HOME=/qa/state XDG_CACHE_HOME=/qa/cache TMPDIR=/qa/tmp
export OPENCODE_CONFIG_CONTENT='{"$schema":"https://opencode.ai/config.json","autoupdate":false,"share":"disabled","plugin":[],"permission":{"*":"allow"},"agent":{"build":{"prompt":"Run only the selected local fixture tool."}}}'
export OPENCODE_DISABLE_PROJECT_CONFIG=true OPENCODE_DISABLE_AUTOUPDATE=true
write_stage manifest_checking
if ! timeout -k 2 8 python3 - <<'PREFLIGHT'
import hashlib, json, sys
release=json.load(open('/etc/habab-android-release.json',encoding='utf-8'))
digest=hashlib.sha256()
with open('/opt/habab-opencode/opencode','rb') as source:
    for block in iter(lambda: source.read(1024*1024), b''):
        digest.update(block)
if release.get('opencodeVersion') != '1.14.21' or release.get('opencodeSha256') != digest.hexdigest():
    raise SystemExit(1)
PREFLIGHT
then printf '{"kind":"issue21_pinned_browser_tool","status":"FAIL","stage":"pinned_binary_manifest","exitCode":32}\n' > /qa/receipt.json; exit 32; fi
write_stage manifest_verified
params=__PARAMS__
set +e
write_stage opencode_invoking
timeout -k 3 60 /opt/habab-opencode/opencode debug agent build --tool habab_browser --params "$params" >/qa/opencode.stdout 2>/qa/opencode.stderr
code=$?
set -e
write_stage opencode_returned
python3 - "$code" <<'PYRESULT'
import json, pathlib, re, sys
code=int(sys.argv[1]); out=pathlib.Path('/qa/opencode.stdout').read_text(errors='replace'); err=pathlib.Path('/qa/opencode.stderr').read_text(errors='replace')
strings=[out,err]
def walk(value):
    if isinstance(value,str):
        strings.append(value)
        try: walk(json.loads(value))
        except Exception: pass
    elif isinstance(value,dict):
        for item in value.values(): walk(item)
    elif isinstance(value,list):
        for item in value: walk(item)
for source in (out,err):
    try: walk(json.loads(source))
    except Exception: pass
joined='\n'.join(strings)
passed=(code==0 and 'Issue21 local fixture' in joined and 'done:issue21 local browser fixture' in joined and '"ok":true' in joined and joined.count('completed')>=3)
receipt={'kind':'issue21_pinned_browser_tool','status':'PASS' if passed else 'FAIL','stage':'complete' if passed else 'pinned_tool_execution','exitCode':code,'loaderVersion':'1.14.21','fixtureTitleRead':'Issue21 local fixture' in joined,'typedValueRead':'done:issue21 local browser fixture' in joined,'toolResultPresent':'"ok":true' in joined,'completedActionCount':min(3,joined.count('completed'))}
if not passed:
    receipt['errorClass']=next((name for name in ('ENOENT','EACCES','ETIMEDOUT','SyntaxError','TypeError','ReferenceError','Error') if re.search(r'\b'+name+r'\b',joined)),None)
pathlib.Path('/qa/receipt.json').write_text(json.dumps(receipt,separators=(',',':')))
PYRESULT
write_stage receipt_written
cat /qa/receipt.json
GUEST
write_stage fixture_script_staged
export LD_LIBRARY_PATH="$native_dir" PROOT_LOADER="$native_dir/libproot-loader.so" PROOT_TMP_DIR="$tmpbase" PROOT_NO_SECCOMP=1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 TZ=Europe/Moscow SSL_CERT_FILE=/etc/ssl/cert.pem HOME=/root TMPDIR=/tmp
write_stage proot_starting
if [ ! -x /system/bin/toybox ]; then emit FAIL proot_timeout_unavailable 26; exit 26; fi
write_stage proot_invoked
set +e
/system/bin/toybox timeout -k 3 72 "$native_dir/libproot.so" --kill-on-exit -0 -r "$runtime" -b /dev -b /proc -b "$test_dir/tmp:/tmp" -b "$test_dir:/qa" -w /qa/workspace /bin/sh /qa/guest-test.sh >"$test_dir/proot.log" 2>&1
code=$?
set -e
write_stage proot_finished
write_stage cleanup_started
printf '{"kind":"issue21_pinned_browser_stages","markers":"%s"}\n' "$(stage_list)"
proot_error_class=EMPTY
if [ -s "$test_dir/proot.log" ]; then
    if grep -Eiq '__BIND_OR_MOUNT_PATTERN__' "$test_dir/proot.log"; then proot_error_class=BIND_OR_MOUNT
    elif grep -Eiq '__GUEST_EXEC_PATTERN__' "$test_dir/proot.log"; then proot_error_class=GUEST_EXEC
    else proot_error_class=OTHER
    fi
fi
if [ "$code" -eq 124 ] || [ "$code" -eq 137 ]; then emit_proot TIMEOUT proot_timeout "$code" "$proot_error_class"
elif [ -f "$receipt" ]; then cat "$receipt"
else emit_proot FAIL proot_no_receipt "$code" "$proot_error_class"; fi
exit "$code"
'''


def make_remote(native_dir, parameters, payloads):
    remote = REMOTE_TEMPLATE
    remote = remote.replace("__CONTROL_B64__", base64.b64encode(payloads["browser-control.mjs"]).decode("ascii"))
    remote = remote.replace("__TOOL_B64__", base64.b64encode(payloads["habab_browser.js"]).decode("ascii"))
    remote = remote.replace("__PARAMS__", shlex.quote(json.dumps(parameters, separators=(",", ":"))))
    remote = remote.replace("__FIXTURE_READINESS_PROBE__", fixture_readiness_probe("http://127.0.0.1:18765/fixture"))
    remote = remote.replace("__BIND_OR_MOUNT_PATTERN__", PROOT_ERROR_PATTERNS["BIND_OR_MOUNT"])
    remote = remote.replace("__GUEST_EXEC_PATTERN__", PROOT_ERROR_PATTERNS["GUEST_EXEC"])
    command = ("run-as " + PACKAGE + " sh -c "
               + "'NATIVE_DIR=\"$1\"; export NATIVE_DIR; exec sh -s' sh "
               + shlex.quote(native_dir))
    return command, remote


def sanitized_receipt(result, source_hashes, host_stages):
    lines = [line for line in result.stdout.splitlines() if line.startswith("{") and line.endswith("}")]
    remote_stages = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("kind") == "issue21_pinned_browser_stages":
            remote_stages = [stage for stage in value.get("markers", "").split(",")
                             if stage in REMOTE_STAGES]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("kind") == "issue21_pinned_browser_tool":
            allowed = {"kind", "status", "stage", "exitCode", "loaderVersion", "fixtureTitleRead",
                       "typedValueRead", "toolResultPresent", "completedActionCount", "errorClass", "prootErrorClass",
                       "stageMarkers"}
            receipt = {key: item for key, item in value.items() if key in allowed}
            if receipt.get("prootErrorClass") not in PROOT_ERROR_CLASSES:
                receipt.pop("prootErrorClass", None)
            embedded_stages = receipt.pop("stageMarkers", "")
            if not remote_stages:
                remote_stages = [stage for stage in embedded_stages.split(",") if stage in REMOTE_STAGES]
            if result.returncode != 0 and receipt.get("status") == "PASS":
                receipt.update(status="FAIL", stage="adb_exit", exitCode=result.returncode)
            receipt["sourceSha256"] = source_hashes
            receipt["hostStageMarkers"] = [stage for stage in host_stages if stage in HOST_STAGES]
            receipt["remoteStageMarkers"] = remote_stages
            return receipt
    return {"kind": "issue21_pinned_browser_tool", "status": "FAIL",
            "stage": "adb_receipt_missing", "exitCode": result.returncode,
            "errorClass": "NoStructuredReceipt", "sourceSha256": source_hashes,
            "hostStageMarkers": [stage for stage in host_stages if stage in HOST_STAGES],
            "remoteStageMarkers": remote_stages}


def save_receipt(receipt):
    directory = ROOT / "habab/android/qa/browser"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"issue21-pinned-browser-tool-{stamp}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"at": datetime.now(timezone.utc).isoformat(), **receipt}, output,
                  ensure_ascii=False, separators=(",", ":"))
        output.write("\n")
    return path


def check_run_as_stdin_fixture(payloads):
    with tempfile.TemporaryDirectory(prefix="issue21-run-as-fixture-") as directory:
        root = Path(directory)
        bindir = root / "bin"
        bindir.mkdir()
        run_as = bindir / "run-as"
        run_as.write_text('#!/bin/sh\n[ "$1" = "ru.habab.home" ] || exit 91\nshift\nexec "$@"\n',
                          encoding="utf-8")
        run_as.chmod(0o700)
        native_dir = str(root / "native lib")
        command, _ = make_remote(native_dir, {}, payloads)
        script = (
            f'test "$NATIVE_DIR" = {shlex.quote(native_dir)}\n'
            "printf '%s\\n' '{\"kind\":\"local_run_as_fixture\",\"status\":\"PASS\",\"stage\":\"stdin_received\"}'\n"
        )
        environment = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
        result = subprocess.run(["sh", "-c", command], input=script, text=True,
                                capture_output=True, env=environment, timeout=5)
        try:
            receipt = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return False
        return (result.returncode == 0 and not result.stderr
                and receipt == {"kind": "local_run_as_fixture", "status": "PASS",
                                "stage": "stdin_received"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="Freshly verified Huawei P30 serial; no device list is queried.")
    parser.add_argument("--adb", default="/opt/android-sdk/platform-tools/adb")
    parser.add_argument("--dry-run", action="store_true", help="Validate generated shell without contacting ADB.")
    args = parser.parse_args()

    parameters = {"url": "http://127.0.0.1:18765/fixture", "actions": [
        {"type": "type", "selector": "#query", "text": "issue21 local browser fixture"},
        {"type": "click", "selector": "#go"},
        {"type": "wait", "selector": '#result[data-state="done"]', "timeout_ms": 2500},
    ]}
    source = ROOT / "habab/distribution/src/habab_home/opencode_tools"
    payloads = {name: (source / name).read_bytes() for name in TOOL_FILES}
    hashes = {name: __import__("hashlib").sha256(data).hexdigest() for name, data in payloads.items()}
    command, remote = make_remote("/data/app/package/lib/arm64", parameters, payloads)
    if args.dry_run:
        guest = re.search(r"""cat > "\$test_dir/guest-test\.sh" <<'GUEST'\n(.*?)\nGUEST\n""", remote, re.S)
        if not guest:
            print(json.dumps({"status": "FAIL", "stage": "guest_script_extract"}))
            return 2
        for label, source in (("remote_shell", remote), ("guest_shell", guest.group(1))):
            result = subprocess.run(["sh", "-n"], input=source, text=True, capture_output=True, timeout=5)
            if result.returncode:
                print(json.dumps({"status": "FAIL", "stage": label + "_syntax", "exitCode": result.returncode}))
                return 2
        if not check_run_as_stdin_fixture(payloads):
            print(json.dumps({"status": "FAIL", "stage": "run_as_stdin_fixture"}))
            return 2
        if not check_fixture_readiness_probe():
            print(json.dumps({"status": "FAIL", "stage": "fixture_readiness_probe"}))
            return 2
        if not check_proot_invocation(remote):
            print(json.dumps({"status": "FAIL", "stage": "proot_invocation"}))
            return 2
        if not check_proot_error_classification():
            print(json.dumps({"status": "FAIL", "stage": "proot_error_classification"}))
            return 2
        if not check_foreground_timeout_runner():
            print(json.dumps({"status": "FAIL", "stage": "foreground_timeout_runner"}))
            return 2
        print(json.dumps({"status": "PASS", "stage": "local_shell_syntax", "adbContacted": False,
                          "remoteShellSyntax": "PASS", "guestShellSyntax": "PASS",
                          "runAsStdinFixture": "PASS", "fixtureReadiness": "PASS",
                          "prootInvocation": "PASS", "prootErrorClassification": "PASS",
                          "foregroundTimeoutRunner": "PASS",
                          "fixture": "loopback_only", "modelCalled": False}))
        return 0
    if not args.serial or not SERIAL_PATTERN.fullmatch(args.serial):
        parser.error("--serial is required and must be a simple device serial")

    adb = [args.adb, "-H", "127.0.0.1", "-P", "18537", "-s", args.serial]
    host_stages = ["host_started", "package_path_lookup_started"]
    try:
        paths = subprocess.run(adb + ["shell", "pm", "path", PACKAGE], capture_output=True,
                               text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        receipt = {"kind": "issue21_pinned_browser_tool", "status": "TIMEOUT", "stage": "package_path_lookup",
                   "sourceSha256": hashes, "hostStageMarkers": host_stages}
        save_receipt(receipt)
        print(json.dumps(receipt, separators=(",", ":")))
        return 1
    host_stages.append("package_path_lookup_finished")
    base_apks = [line.removeprefix("package:").strip() for line in paths.stdout.splitlines()
                 if line.startswith("package:") and line.rstrip().endswith("/base.apk")]
    if paths.returncode or len(base_apks) != 1:
        receipt = {"kind": "issue21_pinned_browser_tool", "status": "BLOCKED",
                   "stage": "base_apk_lookup", "exitCode": paths.returncode, "sourceSha256": hashes,
                   "hostStageMarkers": host_stages}
        save_receipt(receipt)
        print(json.dumps(receipt, separators=(",", ":")))
        return 1

    native_dir = str(Path(base_apks[0]).parent / "lib" / "arm64")
    host_stages.append("package_path_validated")
    command, remote = make_remote(native_dir, parameters, payloads)
    host_stages.append("adb_runtime_started")
    try:
        result = subprocess.run(adb + ["shell", command], input=remote, capture_output=True,
                                text=True, timeout=90)
        host_stages.append("adb_runtime_finished")
        receipt = sanitized_receipt(result, hashes, host_stages)
        if result.returncode != 0 and receipt.get("status") == "PASS":
            receipt.update(status="FAIL", stage="adb_exit", exitCode=result.returncode)
    except subprocess.TimeoutExpired:
        receipt = {"kind": "issue21_pinned_browser_tool", "status": "TIMEOUT",
                   "stage": "adb_runtime_test", "timeoutSeconds": 90, "sourceSha256": hashes,
                   "hostStageMarkers": host_stages}
    except OSError:
        receipt = {"kind": "issue21_pinned_browser_tool", "status": "BLOCKED",
                   "stage": "adb_client", "errorClass": "OSError", "sourceSha256": hashes,
                   "hostStageMarkers": host_stages}

    path = save_receipt(receipt)
    print(json.dumps({**receipt, "receiptPath": str(path)}, separators=(",", ":")))
    return 0 if receipt.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
