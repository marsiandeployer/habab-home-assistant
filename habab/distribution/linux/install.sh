#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
payload="$(cd -- "$script_dir/.." && pwd)"
if [[ ! -f "$payload/release.json" || ! -f "$payload/app/frontend-release.json" ]]; then
  echo "Run this from the extracted habab-home Linux release: payload/linux/install.sh" >&2
  exit 2
fi
exec sudo python3.14 "$script_dir/install.py" "$payload"
