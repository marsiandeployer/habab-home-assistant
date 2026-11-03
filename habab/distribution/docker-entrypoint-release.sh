#!/bin/sh
set -eu

digest_file=/opt/habab-opencode/opencode.sha256
if [ ! -r "$digest_file" ]; then
    echo "Pinned OpenCode checksum is missing from this release image." >&2
    exit 1
fi
HABAB_OPENCODE_SHA256=$(cat "$digest_file")
export HABAB_OPENCODE_SHA256
codex_digest_file=/opt/habab-codex/codex.sha256
if [ -r "$codex_digest_file" ]; then
    HABAB_CODEX_SHA256=$(cat "$codex_digest_file")
    export HABAB_CODEX_SHA256
fi

exec /usr/local/bin/habab-home-entrypoint "$@"
