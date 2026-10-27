#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: sudo $0 /path/to/habab-home-linux-arm64.tar.gz" >&2
  exit 2
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec sudo python3.14 "$script_dir/update.py" "$1"
