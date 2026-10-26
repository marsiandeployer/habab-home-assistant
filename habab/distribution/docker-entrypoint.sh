#!/bin/sh
set -eu

config_file=/config/configuration.yaml
if [ ! -e "$config_file" ] && [ ! -L "$config_file" ]; then
    mkdir -p /config
    if [ ! -e /config/automations.yaml ] && [ ! -L /config/automations.yaml ]; then
        printf '[]\n' > /config/automations.yaml
    fi
    cp /usr/local/share/habab-home/configuration.yaml "$config_file"
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec /init
