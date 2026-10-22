"""Cancel a real OpenCode bash tool while it is waiting, then verify no write."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import websocket

parser = argparse.ArgumentParser()
parser.add_argument("--runtime", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
assert json.loads((args.runtime / "qa-port.json").read_text())["port"] == 8395
token = json.loads((args.runtime / "qa-auth.json").read_text())["access_token"]
ws = websocket.create_connection("ws://127.0.0.1:8395/api/websocket", timeout=220)
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": token}))
assert json.loads(ws.recv())["type"] == "auth_ok"
ws.send(json.dumps({"id": 1, "type": "habab_home/agent/session"}))
session_id = json.loads(ws.recv())["result"]["session_id"]
ws.send(json.dumps({"id": 2, "type": "habab_home/agent/run", "session_id": session_id,
    "text": "Это проверка остановки. Выполни одной командой sleep 25; затем запиши stopped-too-late в /config/habab-agent-cancel.txt. Не делай других действий."}))
report = {}
try:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        processes = subprocess.check_output(["docker", "exec", "habab-setup-agent-qa", "ps", "-o", "args"], text=True)
        if "sleep 25" in processes:
            break
        time.sleep(.5)
    else:
        raise AssertionError("OpenCode did not start the test tool")
    ws.send(json.dumps({"id": 3, "type": "habab_home/agent/run", "session_id": session_id, "text": "second request"}))
    ws.send(json.dumps({"id": 4, "type": "habab_home/agent/abort", "session_id": session_id}))
    replies = {}
    while not {2, 3, 4}.issubset(replies):
        result = json.loads(ws.recv())
        if result.get("type") == "result":
            replies[result["id"]] = result
    assert replies[4]["success"]
    assert not replies[3]["success"] and replies[3]["error"]["code"] == "busy"
    time.sleep(1)
    processes = subprocess.check_output(["docker", "exec", "habab-setup-agent-qa", "ps", "-o", "args"], text=True)
    assert "sleep 25" not in processes
    assert subprocess.run(["docker", "exec", "habab-setup-agent-qa", "test", "!", "-e", "/config/habab-agent-cancel.txt"]).returncode == 0
    report = {"status": "PASS", "real_tool_started": True, "child_process_stopped": True,
              "write_prevented": True, "concurrent_request_rejected": True}
finally:
    ws.close()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
