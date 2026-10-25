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
docker build --pull=false --tag habab-home:0.3.4 \
    --build-arg "HABAB_SOURCE_REVISION=$source_revision" \
    --build-arg "HABAB_SOURCE_DIRTY=$source_dirty" \
    "$script_dir"
