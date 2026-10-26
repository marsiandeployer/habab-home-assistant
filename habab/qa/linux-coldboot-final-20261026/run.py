"""Run one bounded cold start of the existing image on an isolated network."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
IMAGE = "habab-home:linux-smoke-3bdc03-20261026"
EXPECTED_SOURCE = "3bdc03b6d13f41d142550703e698591bf032563d"
HELPER = HERE / "container_probe.py"
RECEIPT = HERE / "receipt.json"
TIMEOUT = 180
TAG = uuid.uuid4().hex[:10]
CONTAINER = "habab-coldboot-final-" + TAG
NETWORK = "habab-coldboot-final-" + TAG
CONTAINER_ID = None
NETWORK_ID = None
TMP = None
started = time.monotonic()
report = {"fixture": "single_disposable_linux_coldboot", "image": IMAGE,
          "expected_source": EXPECTED_SOURCE, "network_mode": "isolated_internal",
          "ports_published": False, "devices_attached": False, "credentials_persisted": False}


def run(args, *, timeout=12, input_text=None):
    return subprocess.run(args, check=False, text=True, input=input_text,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def metadata():
    result = run(["docker", "image", "inspect", IMAGE, "--format",
                  "{{.Id}}|{{json .Config.Entrypoint}}|{{json .Config.Cmd}}|{{.Config.WorkingDir}}|{{index .Config.Labels \"org.opencontainers.image.revision\"}}"])
    if result.returncode:
        raise RuntimeError("image_inspect_failed")
    bits = result.stdout.strip().split("|", 4)
    report["image_id"] = bits[0]
    report["entrypoint"] = json.loads(bits[1])
    report["cmd"] = json.loads(bits[2])
    report["working_dir"] = bits[3]
    report["image_source_label"] = bits[4]


def safe_log_summary():
    markers = {
        "habab_home": r"habab_home",
        "google_genai": r"google[-_]genai",
        "requirements": r"requirements",
        "dependency": r"dependency",
        "setup_py": r"setup\.py",
        "config_flow": r"config flow",
        "setup_error": r"failed to set up|error setting up",
    }
    result = run(["docker", "logs", "--tail", "400", CONTAINER_ID], timeout=8)
    if result.returncode:
        return {"available": False, "marker_counts": {name: 0 for name in markers},
                "matching_line_count": 0, "error_type_names": []}
    lines = result.stdout.splitlines()
    selected = [line for line in lines if re.search(
        r"habab_home|google[-_]genai|requirements|dependency|setup\.py|config flow|failed to set up|error setting up",
        line, re.I)]
    marker_counts = {name: sum(len(re.findall(pattern, line, re.I)) for line in lines)
                     for name, pattern in markers.items()}
    error_types = sorted({match for line in selected
                          for match in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\b", line)})
    return {"available": True, "marker_counts": marker_counts,
            "matching_line_count": len(selected), "error_type_names": error_types}


def save():
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


try:
    metadata()
    if report["entrypoint"] != ["/usr/local/bin/habab-home-entrypoint"] or report["cmd"] != ["/init"]:
        raise RuntimeError("unexpected_image_entrypoint_or_cmd")
    TMP = Path(tempfile.mkdtemp(prefix="fixture-", dir=HERE))
    os.chmod(TMP, 0o700)
    config_dir = TMP / "config"
    config_dir.mkdir(mode=0o700)
    report["empty_config_before_start"] = not any(config_dir.iterdir())
    net = run(["docker", "network", "create", "--internal", NETWORK], timeout=10)
    if net.returncode:
        raise RuntimeError("isolated_network_create_failed")
    NETWORK_ID = net.stdout.strip()
    if not NETWORK_ID:
        raise RuntimeError("isolated_network_id_missing")
    created = run(["docker", "create", "--name", CONTAINER, "--network", NETWORK,
                   "--cpus=1", "--memory=1g", "--security-opt", "no-new-privileges",
                   "-v", str(config_dir) + ":/config", IMAGE], timeout=15)
    if created.returncode:
        raise RuntimeError("container_create_failed")
    CONTAINER_ID = created.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", CONTAINER_ID, re.I):
        CONTAINER_ID = None
        raise RuntimeError("container_id_missing")
    report["container_created"] = True
    result = run(["docker", "start", CONTAINER], timeout=12)
    if result.returncode:
        raise RuntimeError("container_start_failed")
    report["container_started"] = True
    budget = max(5, min(155, TIMEOUT - (time.monotonic() - started) - 22))
    probe = run(["docker", "exec", "-i", CONTAINER, "python3", "-"],
                timeout=budget, input_text=HELPER.read_text())
    if probe.stdout.strip():
        try:
            report.update(json.loads(probe.stdout.strip().splitlines()[-1]))
        except json.JSONDecodeError:
            report["probe_output"] = "unparseable"
    if probe.returncode:
        report["probe_exit_code"] = probe.returncode
    if report.get("habab_home_component") is not True or (report.get("feedback_delivery_status") or {}).get("code") == "unknown_command":
        report["startup_error_summary"] = safe_log_summary()
except subprocess.TimeoutExpired:
    report["run_status"] = "TIMEOUT"
except Exception as exc:
    report["run_status"] = "FAIL"
    report["runner_error"] = type(exc).__name__
finally:
    report["cleanup"] = {"container_removed": False, "network_removed": False,
                         "temporary_config_removed": False, "image_preserved": True}
    if CONTAINER_ID is not None:
        if run(["docker", "inspect", CONTAINER_ID, "--format", "{{.State.Status}}"], timeout=4).returncode == 0:
            run(["docker", "stop", "--time", "10", CONTAINER_ID], timeout=14)
            terminal = run(["docker", "wait", CONTAINER_ID], timeout=5)
            report["container_terminal_exit"] = terminal.stdout.strip() if terminal.returncode == 0 else "wait_timeout"
            removed = run(["docker", "rm", "-f", CONTAINER_ID], timeout=8)
            report["cleanup"]["container_removed"] = removed.returncode == 0
    if NETWORK_ID is not None:
        removed_network = run(["docker", "network", "rm", NETWORK_ID], timeout=8)
        report["cleanup"]["network_removed"] = removed_network.returncode == 0
    if TMP and TMP.exists():
        shutil.rmtree(TMP)
        report["cleanup"]["temporary_config_removed"] = not TMP.exists()
    if not report.get("run_status"):
        good = report.get("readiness") == "PASS"
        report["run_status"] = "PASS" if good and report.get("habab_home_component") and report.get("current_user", {}).get("is_admin") and report.get("feedback_delivery_status", {}).get("status") == "ok" else "FAIL_OR_UNVERIFIED"
    save()

print(json.dumps({"run_status": report.get("run_status"),
                  "image_id": report.get("image_id"),
                  "api_config_first_state": report.get("api_config_first_state"),
                  "api_config_final_state": report.get("api_config_final_state"),
                  "habab_home_component": report.get("habab_home_component"),
                  "feedback_delivery_status": report.get("feedback_delivery_status"),
                  "cleanup": report.get("cleanup"), "receipt": str(RECEIPT)}, ensure_ascii=False))
