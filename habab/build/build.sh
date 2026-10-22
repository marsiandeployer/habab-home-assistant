#!/bin/sh
set -eu
habab_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
frontend_dir=$(CDPATH= cd -- "$habab_dir/.." && pwd)
export HABAB_DEMO_RELEASE=${HABAB_DEMO_RELEASE:-git-$(git -C "$frontend_dir" rev-parse --short=10 HEAD)}
if [ -e "$habab_dir/public/releases/$HABAB_DEMO_RELEASE" ]; then
  echo 'Release already exists. Choose a new HABAB_DEMO_RELEASE; published files must remain immutable.' >&2
  exit 1
fi
# Build the checked-out fork; never reapply the initial archive patch.
mkdir -p "$frontend_dir/translations/frontend"
cp "$habab_dir/build/source/ru.json" "$frontend_dir/translations/frontend/ru.json"
cd "$frontend_dir"
timeout 600s npx --yes --package=node@24.22.0 --package=pnpm@12.14.0 pnpm install --frozen-lockfile
timeout 1200s env SKIP_FETCH_NIGHTLY_TRANSLATIONS=1 NODE_OPTIONS=--max-old-space-size=8192 npx --yes --package=node@24.22.0 --package=pnpm@12.14.0 ./demo/script/build_demo
cd "$habab_dir"
python3 build/publish_build.py
