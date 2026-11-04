#!/usr/bin/env python3
"""Small CLI for native HA actions during an authorized setup-agent turn."""
import json
import os
import sys
import urllib.error
import urllib.request

if len(sys.argv) != 2:
    raise SystemExit("Usage: habab-ha '{\"action\":\"integrations\"}'")
try:
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1) if sys.argv[1] == "-" else sys.argv[1]
    if len(raw if isinstance(raw, bytes) else raw.encode()) > 4 * 1024 * 1024:
        raise SystemExit("Request too large")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise SystemExit("JSON object required")
except ValueError:
    raise SystemExit("Invalid JSON")
token = os.environ.get("HABAB_HA_CONTROL_TOKEN")
if not token:
    raise SystemExit("Available only inside an authorized assistant turn")
request = urllib.request.Request("http://127.0.0.1:8123/api/habab_home/setup-actions",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Habab-Agent-Token": token})
try:
    with urllib.request.urlopen(request, timeout=330 if body.get("action") == "native_api" and (body.get("save_to") or body.get("files")) else 45) as response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise SystemExit("Response too large")
        print(data.decode())
        if body.get("action") == "native_api" and json.loads(data).get("ok") is False:
            raise SystemExit(1)
except urllib.error.HTTPError as error:
    print(json.dumps({"error": "home_assistant_action_failed", "status": error.code}))
    raise SystemExit(1)
except (urllib.error.URLError, TimeoutError):
    raise SystemExit("Home Assistant action unavailable")
