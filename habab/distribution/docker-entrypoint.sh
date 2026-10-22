#!/bin/sh
set -eu

config_file=/config/configuration.yaml
if [ ! -e "$config_file" ]; then
    mkdir -p /config
    cp /usr/local/share/habab-home/configuration.yaml "$config_file"
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec /init
