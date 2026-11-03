#!/bin/sh
set -eu

if [ -z "${HABAB_OPENCODE_SHA256:-}" ] && [ -r /opt/habab-opencode/opencode.sha256 ]; then
    HABAB_OPENCODE_SHA256=$(cat /opt/habab-opencode/opencode.sha256)
    export HABAB_OPENCODE_SHA256
fi
if [ -z "${HABAB_CODEX_SHA256:-}" ] && [ -r /opt/habab-codex/codex.sha256 ]; then
    HABAB_CODEX_SHA256=$(cat /opt/habab-codex/codex.sha256)
    export HABAB_CODEX_SHA256
fi

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
