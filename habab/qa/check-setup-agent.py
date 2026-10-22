"""Live DeepSeek/OpenCode acceptance, only against our isolated QA instance."""
import argparse
import json
from pathlib import Path
import subprocess

import websocket

parser = argparse.ArgumentParser()
parser.add_argument("--runtime", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
port = json.loads((args.runtime / "qa-port.json").read_text())["port"]
if port != 8395:
    raise SystemExit("This check is scoped to isolated QA port 8395")
auth = json.loads((args.runtime / "qa-auth.json").read_text())
ws = websocket.create_connection(f"ws://127.0.0.1:{port}/api/websocket", timeout=220)
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": auth["access_token"]}))
assert json.loads(ws.recv())["type"] == "auth_ok"
sequence = 0


def call(command, **data):
    global sequence
    sequence += 1
    ws.send(json.dumps({"id": sequence, "type": command, **data}))
    while True:
        result = json.loads(ws.recv())
        if result.get("id") == sequence:
            return result


report = {"environment": "isolated Docker HA + read-only OpenCode 1.14.21 + live DeepSeek", "cases": []}
try:
    session = call("habab_home/agent/session")
    assert session["success"], session.get("error", {}).get("code")
    session_id = session["result"]["session_id"]
    denied = call("habab_home/agent/run", session_id="ses_unowned", text="test")
    assert not denied["success"] and denied["error"]["code"] == "not_found"
    report["cases"].append({"name": "foreign_session_denied", "pass": True})
    result = call("habab_home/agent/run", session_id=session_id, text=(
        "Проверь свою среду реальными командами: id -u, наличие /.dockerenv, "
        "доступность /var/run/docker.sock, /run/dbus/system_bus_socket и bluetoothctl. "
        "Создай /config/habab-agent-check.txt с единственной строкой root-inside-docker. "
        "Не читай настройки и секреты. Кратко сообщи фактический результат и сможешь ли сканировать Bluetooth."
    ))
    assert result["success"], result.get("error", {}).get("code")
    output = subprocess.check_output(["docker", "exec", "habab-setup-agent-qa", "cat", "/config/habab-agent-check.txt"], text=True)
    assert output.strip() == "root-inside-docker"
    report["cases"].append({"name": "actual_container_write", "pass": True})
    report["answer"] = result["result"]["messages"][-1]["text"]
    restored = call("habab_home/agent/session")
    assert restored["success"] and restored["result"]["session_id"] == session_id
    assert restored["result"]["messages"] == result["result"]["messages"]
    report["cases"].append({"name": "history_readback", "pass": True})
    report["status"] = "PASS"
finally:
    ws.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"status": report.get("status", "PARTIAL"), "cases": report["cases"]}, ensure_ascii=False))
