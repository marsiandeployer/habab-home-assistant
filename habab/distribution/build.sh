#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
provenance=$(python3 "$script_dir/source_provenance.py" \
    --root "$script_dir" --release-file "$script_dir/release.json")
set -- $provenance
if [ "$#" -ne 2 ]; then
    echo "Unable to resolve source provenance." >&2
    exit 1
fi
source_revision=$1
source_dirty=$2
case "$source_revision" in
    unknown) ;;
    *[!0-9a-f]*|'')
        echo "Invalid source revision metadata." >&2
        exit 1
        ;;
    *)
        revision_length=${#source_revision}
        if [ "$revision_length" -lt 40 ] || [ "$revision_length" -gt 64 ]; then
            echo "Invalid source revision metadata." >&2
            exit 1
        fi
        ;;
    esac
case "$source_dirty" in
    true|false|unknown) ;;
    *) echo "Invalid source dirty metadata." >&2; exit 1 ;;
esac
canonical_tunnel="$script_dir/../product-tunnel/web_tunnel.py"
stage_dir="$script_dir/product-tunnel"
staged_tunnel="$stage_dir/web_tunnel.py"
if [ ! -f "$canonical_tunnel" ]; then
    echo "Canonical product tunnel source is missing: $canonical_tunnel" >&2
    exit 1
fi
stage_created=false
if [ -f "$staged_tunnel" ]; then
    if ! cmp -s "$canonical_tunnel" "$staged_tunnel"; then
        echo "A stale staged tunnel source exists at $staged_tunnel" >&2
        exit 1
    fi
else
    mkdir -p "$stage_dir"
    cp "$canonical_tunnel" "$staged_tunnel"
    stage_created=true
fi
cleanup_stage() {
    if [ "$stage_created" = true ]; then
        rm -f "$staged_tunnel"
        rmdir "$stage_dir" 2>/dev/null || true
    fi
}
trap cleanup_stage EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
docker build --pull=false --tag habab-home:0.3.6 \
    --build-arg "HABAB_SOURCE_REVISION=$source_revision" \
    --build-arg "HABAB_SOURCE_DIRTY=$source_dirty" \
    "$script_dir"
