#!/usr/bin/env python3
"""Hourly PM2 task: retain consented telemetry for at most sixty days."""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv
import psycopg

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from habab_demo.telemetry import cleanup

load_dotenv(root.parent / '.env')
with psycopg.connect(os.environ['HA_DEMO_DATABASE_DSN'], connect_timeout=5) as connection:
    cleanup(connection)
print('Telemetry retention cleanup complete')
