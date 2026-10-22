#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker build --pull=false --tag habab-home:0.1.0 "$script_dir"
