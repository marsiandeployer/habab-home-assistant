"""Real HA accounts: deny non-admins and another administrator's session."""
import argparse
import json
from pathlib import Path
import secrets

import requests
import websocket

parser = argparse.ArgumentParser()
parser.add_argument("--runtime", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
port = json.loads((args.runtime / "qa-port.json").read_text())["port"]
assert port == 8395
base = f"http://127.0.0.1:{port}"
auth = json.loads((args.runtime / "qa-auth.json").read_text())


def connect(token):
    ws = websocket.create_connection(base.replace("http", "ws") + "/api/websocket", timeout=30)
    ws.recv()
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    assert json.loads(ws.recv())["type"] == "auth_ok"
    return ws


counter = 0


def call(ws, command, **data):
    global counter
    counter += 1
    ws.send(json.dumps({"id": counter, "type": command, **data}))
    while True:
        result = json.loads(ws.recv())
        if result.get("id") == counter:
            return result


owner = connect(auth["access_token"])
created = []
cases = []
try:
    own_session = call(owner, "habab_home/agent/session")["result"]["session_id"]
    for role, group in [("viewer", "system-read-only"), ("admin", "system-admin")]:
        username = f"qa-{role}-{secrets.token_hex(3)}"
        password = secrets.token_urlsafe(24)
        result = call(owner, "config/auth/create", name=username, group_ids=[group], local_only=True)
        assert result["success"]
        user_id = result["result"]["user"]["id"]
        created.append(user_id)
        assert call(owner, "config/auth_provider/homeassistant/create", user_id=user_id,
                    username=username, password=password)["success"]
        flow = requests.post(base + "/auth/login_flow", json={"client_id": base + "/",
            "handler": ["homeassistant", None], "redirect_uri": base + "/?auth_callback=1"}, timeout=15).json()
        login = requests.post(base + "/auth/login_flow/" + flow["flow_id"],
            json={"client_id": base + "/", "username": username, "password": password}, timeout=15).json()
        token = requests.post(base + "/auth/token", data={"grant_type": "authorization_code",
            "client_id": base + "/", "code": login["result"]}, timeout=15).json()["access_token"]
        other = connect(token)
        try:
            commands = [("habab_home/agent/run", {"session_id": own_session, "text": "test"}),
                        ("habab_home/agent/abort", {"session_id": own_session})]
            if role == "viewer":
                commands.append(("habab_home/agent/session", {}))
            for command, data in commands:
                result = call(other, command, **data)
                expected = "unauthorized" if role == "viewer" else "not_found"
                assert not result["success"] and result["error"]["code"] == expected
                cases.append({"role": role, "command": command, "code": expected, "pass": True})
        finally:
            other.close()
finally:
    for user_id in created:
        call(owner, "config/auth/delete", user_id=user_id)
    owner.close()
    args.output.write_text(json.dumps({"cases": cases, "temporary_users_removed": len(created)}, indent=2) + "\n")
print(f"Access checks passed: {len(cases)}; temporary accounts removed: {len(created)}")
