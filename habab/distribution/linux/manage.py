#!/usr/bin/env python3
"""Small installed command for status, service control, updates and rollback."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from install import rollback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "start", "stop", "logs", "rollback", "update"))
    parser.add_argument("package", nargs="?", help="Local release archive for update")
    args = parser.parse_args()
    if args.command == "status":
        current = Path("/opt/habab-home/current")
        metadata = json.loads((current / "source.json").read_text()) if current.exists() else None
        active = subprocess.run(["systemctl", "is-active", "habab-home.service"],
                                text=True, capture_output=True).stdout.strip()
        print(json.dumps({"active": active, "release": metadata}, sort_keys=True))
    elif args.command in ("start", "stop", "logs"):
        if args.command == "logs":
            os.execvp("journalctl", ["journalctl", "-u", "habab-home.service", "-f"])
        subprocess.run(["systemctl", args.command, "habab-home.service"], check=True)
    elif args.command == "rollback":
        print(json.dumps({"active_release": rollback()}))
    elif args.command == "update":
        if not args.package:
            parser.error("update requires the path to a verified release archive")
        subprocess.run([sys.executable, str(HERE / "update.py"), args.package], check=True)


if __name__ == "__main__":
    main()
