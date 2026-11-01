#!/usr/bin/env python3
"""Check an unchanged public ARM64 APK on an isolated Android emulator."""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

REPOSITORY = "marsiandeployer/habab-home-assistant"
ASSET = "habab-home-arm64.apk"
PACKAGE = "ru.habab.home"
COMPONENT = PACKAGE + "/.MainActivity"
SERIAL = "emulator-5554"


class Failure(Exception):
    def __init__(self, category, code, diagnostics=None):
        self.category, self.code = category, code
        self.diagnostics = diagnostics
        super().__init__(code)


def require(condition, category, code):
    if not condition:
        raise Failure(category, code)


def fresh_onboarding(value):
    """Require the real Core onboarding API to offer the unfinished owner step."""
    return isinstance(value, list) and any(
        isinstance(step, dict) and step.get("step") == "user" and step.get("done") is False
        for step in value)


def notification_allow_coordinates(ui_xml):
    """Find the platform permission button without depending on screen size or language."""
    try:
        root = ET.fromstring(ui_xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        if node.get("resource-id", "").endswith(":id/permission_allow_button"):
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def run(command, timeout=30, input=None, check=True):
    tool = {"tool": Path(command[0]).name}
    subcommand = 3 if tool["tool"] == "adb" and len(command) > 3 and command[1] == "-s" else 1
    if len(command) > subcommand and re.fullmatch(r"[-a-z_]+", str(command[subcommand])):
        tool["subcommand"] = str(command[subcommand])
    try:
        result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure("INFRA_UNAVAILABLE", "tool_timed_out",
                      {**tool, "timeout_seconds": timeout}) from None
    except OSError:
        raise Failure("INFRA_UNAVAILABLE", "tool_unavailable", tool) from None
    if check and result.returncode:
        raise Failure("INFRA_UNAVAILABLE", "tool_exit_nonzero", {
            **tool, "exit_code": result.returncode,
            "output_tail": safe_log((result.stdout + "\n" + result.stderr)[-4000:])})
    return result


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        require(url.scheme == "https" and url.hostname in {
            "github.com", "release-assets.githubusercontent.com"
        } and url.username is None and url.password is None and not url.fragment,
                "ARTIFACT_FAILED", "untrusted_download_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_open(open_url, request, timeout):
    """Retry a public HTTP open once, only for 429 or server errors."""
    for attempt in range(2):
        try:
            return open_url(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if attempt or not (error.code == 429 or 500 <= error.code <= 599):
                raise
            print("Public release HTTP retry: status=" + str(error.code) + " attempt=2/2")
            time.sleep(2)


def download(tag, digest, size, destination):
    """No token or credential is read; explicit public release and caller pins are mandatory."""
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag)) and tag != "latest",
            "INPUT_FAILED", "invalid_or_mutable_release_tag")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "INPUT_FAILED", "invalid_sha256")
    require(0 < size < 2 ** 31, "INPUT_FAILED", "invalid_apk_size")
    require(not destination.exists() and not destination.is_symlink(),
            "INPUT_FAILED", "output_apk_already_exists")
    endpoint = "https://api.github.com/repos/" + REPOSITORY + "/releases/tags/" + tag
    request = urllib.request.Request(endpoint, headers={"User-Agent": "habab-android-compat-smoke",
                                                       "Accept": "application/vnd.github+json"})
    created = False
    try:
        with public_open(urllib.request.urlopen, request, 30) as response:
            raw = response.read(2_000_001)
        require(len(raw) <= 2_000_000, "ARTIFACT_FAILED", "release_metadata_too_large")
        release = json.loads(raw)
        require(release.get("tag_name") == tag and release.get("draft") is False
                and bool(release.get("published_at")), "ARTIFACT_FAILED", "release_not_public")
        assets = [a for a in release.get("assets", []) if a.get("name") == ASSET]
        require(len(assets) == 1, "ARTIFACT_FAILED", "release_apk_not_unique")
        asset = assets[0]
        require(asset.get("state") == "uploaded" and asset.get("size") == size,
                "ARTIFACT_FAILED", "release_apk_size_mismatch")
        require(type(asset.get("id")) is int and asset["id"] > 0,
                "ARTIFACT_FAILED", "invalid_release_asset_id")
        require(asset.get("digest") in (None, "sha256:" + digest),
                "ARTIFACT_FAILED", "release_apk_digest_mismatch")
        expected_url = "https://github.com/" + REPOSITORY + "/releases/download/" + tag + "/" + ASSET
        require(asset.get("browser_download_url") == expected_url,
                "ARTIFACT_FAILED", "unexpected_release_apk_url")
        opener = urllib.request.build_opener(PublicRedirect())
        hashed, count = hashlib.sha256(), 0
        with public_open(opener.open, expected_url, 60) as response, destination.open("xb") as output:
            created = True
            while block := response.read(1024 * 1024):
                count += len(block)
                require(count <= size, "ARTIFACT_FAILED", "download_exceeds_size")
                hashed.update(block)
                output.write(block)
        require(count == size and hashed.hexdigest() == digest,
                "ARTIFACT_FAILED", "download_bytes_or_digest_mismatch")
        with zipfile.ZipFile(destination) as apk:
            abis = sorted({n.split("/")[1] for n in apk.namelist()
                           if n.startswith("lib/") and n.endswith(".so")})
        require(abis == ["arm64-v8a"], "ARTIFACT_FAILED", "expected_arm64_only_apk")
    except Failure:
        if created:
            destination.unlink(missing_ok=True)
        raise
    except zipfile.BadZipFile:
        if created:
            destination.unlink(missing_ok=True)
        raise Failure("ARTIFACT_FAILED", "invalid_apk_archive") from None
    except urllib.error.HTTPError as error:
        if created:
            destination.unlink(missing_ok=True)
        raise Failure("ARTIFACT_FAILED", "public_release_http_error",
                      {"http_status": int(error.code)}) from None
    except (OSError, ValueError, urllib.error.URLError):
        if created:
            destination.unlink(missing_ok=True)
        # Never emit redirected signed URLs or upstream response bodies.
        raise Failure("ARTIFACT_FAILED", "public_release_download_failed") from None
    return {"tag": tag, "asset_id": asset["id"], "sha256": digest, "bytes": size, "abis": abis}


def system_image_metadata(text):
    values = dict(tuple(part.strip() for part in line.split("=", 1))
                  for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#"))
    return {key: safe_log(values.get(key, ""))[:128] for key in (
        "AndroidVersion.ApiLevel", "SystemImage.Abi", "SystemImage.TagId",
        "SystemImage.VendorId", "Pkg.Revision")}


def cleanup_emulator(process, adb):
    """Reap the owned launcher and kill its session's QEMU child if needed."""
    result = {"emulator_stopped": process is None}
    if process is None:
        return result
    if adb:
        try:
            run([str(adb), "-s", SERIAL, "emu", "kill"], timeout=15, check=False)
        except Failure:
            result["adb_cleanup_unavailable"] = True
    try:
        for action, timeout in ((signal.SIGTERM, 15), (signal.SIGKILL, 10)):
            try:
                os.killpg(process.pid, action)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                continue
        # A launcher can exit before its QEMU child; reap that owned group too.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        result["emulator_stopped"] = process.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        result["emulator_stopped"] = False
    return result


def safe_log(value):
    value = re.sub(r"https?://\S+", "[url removed]", value)
    value = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", value)
    return re.sub(r"(?i)((?:api[_-]?key|access_token|refresh_token|password)\s*[=:]\s*)\S+",
                  r"\1[redacted]", value)


def emulator_tail(path):
    lines = path.read_text(errors="replace").splitlines()[-60:]
    lines = ["[credential-related log line removed]" if re.search(
        r"token|secret|authorization|password|\bkey\b", line, re.I) else line for line in lines]
    return safe_log("\n".join(lines))[-6000:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, choices=("35", "36"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--runtime", action="store_true", help="Also provision the bundled runtime and check fresh HA onboarding")
    args = parser.parse_args()
    os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"status": "RUNNING", "api": int(args.api), "scope": "fresh runtime onboarding" if args.runtime else "offline MainActivity survival",
              "native_runtime_tested": False, "page_size_compatibility_proven": False,
              "microphone_or_audio_tested": False, "workflow_source": os.environ.get("GITHUB_SHA"),
              "screenshots_require_human_review": True}
    emulator_process = None
    emulator_log = None
    adb = None
    owned_apk = None

    def save():
        (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def device(*command, **kwargs):
        return run([str(adb), "-s", SERIAL, *command], **kwargs)

    try:
        stage = "artifact"
        report["stage"] = stage
        try:
            size = int(os.environ.get("HABAB_COMPAT_APK_BYTES", ""))
        except ValueError:
            raise Failure("INPUT_FAILED", "invalid_apk_size") from None
        apk = args.out / ASSET
        report["artifact"] = download(os.environ.get("HABAB_COMPAT_RELEASE_TAG", ""),
                                      os.environ.get("HABAB_COMPAT_APK_SHA256", ""), size, apk)
        owned_apk = apk
        stage = report["stage"] = "host_preflight"
        require(platform.machine() == "x86_64", "INFRA_UNAVAILABLE", "host_not_x86_64")
        sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/missing-sdk")
        adb, emulator = sdk / "platform-tools/adb", sdk / "emulator/emulator"
        manager = sdk / "cmdline-tools/latest/bin/sdkmanager"
        avdmanager = sdk / "cmdline-tools/latest/bin/avdmanager"
        report["missing_sdk_tools"] = [p.name for p in (adb, manager, avdmanager)
                                       if not (p.is_file() and os.access(p, os.X_OK))]
        require(not report["missing_sdk_tools"],
                "INFRA_UNAVAILABLE", "preinstalled_sdk_tools_missing")
        kvm = Path("/dev/kvm")
        require(kvm.is_char_device(), "INFRA_UNAVAILABLE", "kvm_missing")
        if not os.access(kvm, os.R_OK | os.W_OK):
            # Only this fresh hosted VM's existing device; do not install kernel modules.
            run(["sudo", "chgrp", grp.getgrgid(os.getgid()).gr_name, str(kvm)], timeout=10)
            run(["sudo", "chmod", "g+rw", str(kvm)], timeout=10)
        require(os.access(kvm, os.R_OK | os.W_OK), "INFRA_UNAVAILABLE", "kvm_not_accessible")
        require(len(run([str(adb), "devices"]).stdout.strip().splitlines()) == 1,
                "INFRA_UNAVAILABLE", "unexpected_existing_adb_target")
        stage = report["stage"] = "system_image"
        image = "system-images;android-" + args.api + ";google_apis;x86_64"
        run([str(manager), "--licenses"], input="y\n" * 100, timeout=120)
        run([str(manager), "--install", "emulator"], timeout=300)
        require(emulator.is_file() and os.access(emulator, os.X_OK),
                "INFRA_UNAVAILABLE", "installed_emulator_missing")
        qemu = sdk / "emulator/qemu/linux-x86_64/qemu-system-x86_64"
        require(qemu.is_file(), "INFRA_UNAVAILABLE", "installed_qemu_missing")
        report["host_libraries"] = {}
        for binary in (emulator, qemu):
            libraries = run(["ldd", str(binary)])
            report["host_libraries"][binary.name] = re.findall(
                r"^\s*(\S+)\s+=>\s+not found\s*$", libraries.stdout, re.M)
        # Direct ldd cannot resolve libraries that the emulator launcher supplies
        # to QEMU. Record it for diagnosis; the actual launcher check is decisive.
        acceleration = run([str(emulator), "-accel-check"])
        require("KVM" in acceleration.stdout + acceleration.stderr,
                "INFRA_UNAVAILABLE", "kvm_not_reported_by_emulator")
        report["emulator_version"] = run([str(emulator), "-version"]).stdout.splitlines()[:2]
        run([str(manager), "--install", image], timeout=600)
        properties = sdk / "system-images" / ("android-" + args.api) / "google_apis/x86_64/source.properties"
        report["system_image"] = {"package": image,
                                  "properties": system_image_metadata(
                                      properties.read_text() if properties.is_file() else "")}
        avd = "habab_compat_api" + args.api
        avd_home = args.out.resolve() / "avd"
        avd_home.mkdir(exist_ok=True)
        os.environ["ANDROID_AVD_HOME"] = str(avd_home)
        run([str(avdmanager), "create", "avd", "--force", "--name", avd, "--package", image],
            input="no\n", timeout=120)
        if args.runtime:
            with (avd_home / (avd + ".avd") / "config.ini").open("a") as config:
                config.write("\ndisk.dataPartition.size=8G\n")
        emulator_log = (args.out / "emulator-private.log").open("w")
        emulator_process = subprocess.Popen([str(emulator), "-avd", avd, "-port", "5554",
            "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot", "-gpu", "software",
            "-accel", "on", "-memory", "2048", "-cores", "2", "-partition-size", "8192" if args.runtime else "2048",
            "-camera-back", "none", "-camera-front", "none"],
            stdout=emulator_log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 180
        while True:
            require(emulator_process.poll() is None and time.monotonic() < deadline,
                    "INFRA_UNAVAILABLE", "emulator_boot_failed")
            connected = device("get-state", check=False).stdout.strip() == "device"
            if connected and device("shell", "getprop", "sys.boot_completed", check=False).stdout.strip() == "1":
                break
            time.sleep(2)
        stage = report["stage"] = "guest_preflight"
        actual_api = device("shell", "getprop", "ro.build.version.sdk").stdout.strip()
        require(actual_api == args.api, "INFRA_UNAVAILABLE", "guest_api_mismatch")
        report["actual_guest_api"] = int(actual_api)
        props = {name: device("shell", "getprop", name).stdout.strip() for name in (
            "ro.product.cpu.abilist", "ro.dalvik.vm.native.bridge", "ro.enable.native.bridge.exec")}
        require("arm64-v8a" in props["ro.product.cpu.abilist"].split(",")
                and props["ro.dalvik.vm.native.bridge"] not in ("", "0"),
                "INFRA_UNAVAILABLE", "arm64_translator_not_available")
        report["translator"] = props
        report["guest_page_size"] = device("shell", "getconf", "PAGE_SIZE").stdout.strip()
        if not args.runtime:
            # Offline guest: no factory archive, account or voice request can download.
            device("shell", "svc", "wifi", "disable")
            device("shell", "svc", "data", "disable")
            deadline = time.monotonic() + 30
            while not re.search(r"Active default network:\s*(?:none|null)\b",
                                device("shell", "dumpsys", "connectivity").stdout, re.I):
                require(time.monotonic() < deadline, "INFRA_UNAVAILABLE", "guest_network_not_disabled")
                time.sleep(2)
            report["guest_offline_before_app_launch"] = True
        device("logcat", "-c")
        stage = report["stage"] = "application"
        installed = device("install", "--no-streaming", str(apk), timeout=120, check=False)
        require(installed.returncode == 0 and "Success" in installed.stdout,
                "APP_FAILED", "unchanged_apk_install_failed")
        package_dump = device("shell", "dumpsys", "package", PACKAGE).stdout
        code = re.search(r"\bversionCode=(\d+)", package_dump)
        name = re.search(r"\bversionName=([^\s]+)", package_dump)
        require(code is not None and name is not None, "APP_FAILED", "installed_package_metadata_missing")
        report["installed_app"] = {"package": PACKAGE, "version_code": int(code[1]),
                                   "version_name": name[1]}
        launch = ["shell", "am", "start", "-W", "-a", "android.intent.action.MAIN"]
        if args.runtime:
            launch.extend(["-c", "android.intent.category.LAUNCHER"])
        launch.extend(["-n", COMPONENT])
        launched = device(*launch, timeout=60, check=False)
        require(launched.returncode == 0 and "Status: ok" in launched.stdout,
                "APP_FAILED", "main_activity_start_failed")
        if args.runtime:
            # Android can display its own notification-permission dialog when
            # the foreground service starts; handle it as a first-run user would.
            for _ in range(4):
                time.sleep(2)
                device("shell", "uiautomator", "dump", "/sdcard/habab-smoke-ui.xml", check=False, timeout=20)
                ui = device("shell", "cat", "/sdcard/habab-smoke-ui.xml", check=False).stdout
                coords = notification_allow_coordinates(ui)
                if coords:
                    device("shell", "input", "tap", str(coords[0]), str(coords[1]))
                    report["notification_permission_dialog_accepted"] = True
                    break
        time.sleep(5)
        pids = device("shell", "pidof", PACKAGE, check=False).stdout.split()
        activities = device("shell", "dumpsys", "activity", "activities").stdout
        alive = bool(pids) and any(COMPONENT in line for line in activities.splitlines()
                                  if "ResumedActivity" in line or "topResumedActivity" in line)
        raw = device("logcat", "-d", "-v", "threadtime", "-b", "main", "-b", "crash").stdout
        known = set(pids) | set(re.findall(r"Process: " + re.escape(PACKAGE) + r", PID: (\d+)", raw))
        lines = [line for line in raw.splitlines() if (m := re.match(
            r"\d\d-\d\d\s+\S+\s+(\d+)\s+\d+\s+[VDIWEF]\s+", line)) and m[1] in known]
        log = safe_log("\n".join(lines[-2000:]))
        (args.out / "app-logcat.txt").write_text(log + "\n")
        fatal = bool(re.search(r"FATAL EXCEPTION|Fatal signal", log, re.I))
        report["activity_alive"], report["fatal_exception_or_signal"] = alive, fatal
        services = device("shell", "dumpsys", "activity", "services", PACKAGE).stdout
        if not args.runtime:
            require(not any(name in services for name in (".ServerService", ".MaintenanceService", ".VoiceService")),
                    "APP_FAILED", "unexpected_runtime_or_voice_service")
            report["runtime_and_voice_services_absent"] = True
        screenshot = subprocess.run([str(adb), "-s", SERIAL, "exec-out", "screencap", "-p"],
                                    capture_output=True, timeout=30)
        require(screenshot.returncode == 0 and screenshot.stdout.startswith(b"\x89PNG\r\n\x1a\n"),
                "INFRA_UNAVAILABLE", "screenshot_capture_failed")
        (args.out / "screen.png").write_bytes(screenshot.stdout)
        alive = alive and bool(device("shell", "pidof", PACKAGE, check=False).stdout.split())
        report["activity_alive"] = alive
        require(alive and not fatal, "APP_FAILED", "activity_dead_or_fatal_exception")
        if args.runtime:
            stage = report["stage"] = "fresh_home_assistant"
            device("forward", "tcp:18131", "tcp:8123")
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                require(emulator_process.poll() is None, "INFRA_UNAVAILABLE", "emulator_stopped_during_runtime")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:18131/api/onboarding", timeout=5) as response:
                        report["last_onboarding_http_status"] = response.status
                        onboarding = json.loads(response.read(65536))
                    if fresh_onboarding(onboarding):
                        report["native_runtime_tested"] = True
                        report["fresh_user_onboarding_available"] = True
                        break
                except urllib.error.HTTPError as error:
                    report["last_onboarding_http_status"] = error.code
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                time.sleep(5)
            require(report["native_runtime_tested"], "APP_FAILED", "fresh_onboarding_not_available")
        report["status"] = "PASS"
    except Failure as error:
        report.update(status="FAIL", failure_category=error.category, failure_code=error.code)
        if error.diagnostics:
            report["failure_diagnostics"] = error.diagnostics
            print(json.dumps(error.diagnostics))
        print("::error title=Android compatibility smoke::" + error.category + ": " + error.code)
    except Exception as error:
        report.update(status="FAIL", failure_category="RUNNER_FAILED", failure_code=type(error).__name__)
        print("::error title=Android compatibility smoke::RUNNER_FAILED: " + type(error).__name__)
    finally:
        if emulator_process and report["status"] != "PASS":
            report["emulator_exit_code"] = emulator_process.poll()
        report.update(cleanup_emulator(emulator_process, adb))
        if not report["emulator_stopped"] and report["status"] == "PASS":
            report.update(status="FAIL", failure_category="INFRA_UNAVAILABLE",
                          failure_code="emulator_cleanup_failed")
        if emulator_log:
            emulator_log.close()
            if report["status"] != "PASS":
                tail = emulator_tail(args.out / "emulator-private.log")
                (args.out / "emulator-failure.txt").write_text(tail + "\n")
                print(json.dumps({"emulator_failure_tail": tail}))
        save()
        # Public APK bytes and raw emulator output are deliberately not uploaded as evidence.
        if owned_apk is not None:
            owned_apk.unlink(missing_ok=True)
    print("Android compatibility smoke: " + report["status"])
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
