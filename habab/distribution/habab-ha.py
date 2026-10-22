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
    body = json.loads(sys.argv[1])
except ValueError:
    raise SystemExit("Invalid JSON")
token = os.environ.get("HABAB_HA_CONTROL_TOKEN")
if not token:
    raise SystemExit("Available only inside an authorized assistant turn")
request = urllib.request.Request("http://127.0.0.1:8123/api/habab_home/setup-actions",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Habab-Agent-Token": token})
try:
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(1_000_001)
        if len(data) > 1_000_000:
            raise SystemExit("Response too large")
        print(data.decode())
except urllib.error.HTTPError as error:
    print(json.dumps({"error": "home_assistant_action_failed", "status": error.code}))
    raise SystemExit(1)
except (urllib.error.URLError, TimeoutError):
    raise SystemExit("Home Assistant action unavailable")
