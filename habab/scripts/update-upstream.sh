#!/bin/sh
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo_dir"
if [ -n "$(git status --porcelain)" ]; then
  echo 'Commit or stash local changes before updating upstream.' >&2
  exit 1
fi
if [ "$(git branch --show-current)" != 'main' ]; then
  echo 'Switch to main before preparing an upstream update branch.' >&2
  exit 1
fi
if [ "$(git remote get-url upstream)" != 'https://github.com/home-assistant/frontend.git' ]; then
  echo 'Unexpected upstream remote; inspect it before updating.' >&2
  exit 1
fi
git fetch --no-tags upstream dev
update_branch="update/home-assistant-$(date -u +%Y%m%d-%H%M%S)"
git switch -c "$update_branch"
git merge --no-edit upstream/dev
echo "Upstream merged into $update_branch. Run habab/docs/UPDATING.md checks; production was not changed."
