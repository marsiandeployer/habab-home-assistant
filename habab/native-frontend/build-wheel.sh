#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
worktree="${HABAB_NATIVE_FRONTEND_WORKTREE:-/root/.local/share/habab-native-frontend-fonts-build}"
translations="${HABAB_NATIVE_FRONTEND_TRANSLATIONS:-/root/.local/share/habab-native-frontend-translations-official/translations}"
source_commit="26690e06b0bf64a1487929245c32842ef92c62b5"
patch_file="$repo_root/habab/native-frontend/bridge.patch"
patch_sha="9b6f0ef7cffa348bea55cf6ec3299bc692407533ba86ce5114dfdda42199d3fe"
catalog_sha="154cc8ea8822c85d63b46e34ccc667cbac01df2cbf8cacb0a300bfe1fecda3ce"
wheel_name="home_assistant_frontend-20260930.4+habab.4-py3-none-any.whl"
staging="$(mktemp -d /root/.local/share/habab-native-frontend-wheel.XXXXXX)"
pyproject_backup="$staging/pyproject.toml"

cleanup() {
  if [[ -f "$pyproject_backup" ]]; then
    cp "$pyproject_backup" "$worktree/pyproject.toml"
  fi
  rm -rf "$staging"
}
trap cleanup EXIT

[[ "$(git -C "$worktree" rev-parse HEAD)" == "$source_commit" ]] || {
  echo "Worktree must be at pinned frontend commit $source_commit" >&2
  exit 1
}
[[ -f "$patch_file" && "$(sha256sum "$patch_file" | cut -d' ' -f1)" == "$patch_sha" ]] || {
  echo "Bridge patch digest mismatch" >&2
  exit 1
}
translation_catalog_sha() {
  find "$1" -type f -name 'ru-*.json' -print0 | LC_ALL=C sort -z |
    while IFS= read -r -d '' file; do
      printf '%s\0%s\n' "${file#"$1"/}" "$(sha256sum "$file" | cut -d' ' -f1)"
    done | sha256sum | cut -d' ' -f1
}
[[ -d "$translations/app" && "$(translation_catalog_sha "$translations")" == "$catalog_sha" ]] || {
  echo "Pinned Russian translation catalog is missing or changed" >&2
  exit 1
}
[[ -z "$(git -C "$worktree" status --porcelain --untracked-files=all)" ]] || {
  echo "Use a fresh clean worktree; refusing to overwrite local changes" >&2
  exit 1
}

git -C "$worktree" apply --check "$patch_file"
git -C "$worktree" apply "$patch_file"

export NVM_DIR="${NVM_DIR:-/root/.nvm}"
source "$NVM_DIR/nvm.sh"
nvm use 24.21.0 >/dev/null
export COREPACK_HOME="${COREPACK_HOME:-/root/.local/share/habab-native-frontend-tooling/corepack}"
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export SOURCE_DATE_EPOCH="$(git -C "$worktree" show -s --format=%ct HEAD)"
export NODE_ENV=production
cd "$worktree"
corepack enable pnpm
[[ "$(pnpm --version)" == "11.28.0" ]] || {
  echo "Unexpected pnpm version" >&2
  exit 1
}

pnpm install --frozen-lockfile
pnpm exec eslint \
  build-scripts/gulp/gather-static.js \
  src/habab/native-ui-bridge.ts \
  src/dialogs/config-flow/show-dialog-config-flow.ts \
  src/dialogs/config-flow/show-dialog-options-flow.ts \
  src/dialogs/voice-command-dialog/show-ha-voice-command-dialog.ts \
  src/entrypoints/core.ts \
  src/panels/lovelace/hui-root.ts \
  src/data/preloads.ts \
  src/data/recorder.ts \
  src/layouts/home-assistant.ts \
  test/data/recorder-info.test.ts \
  --max-warnings=0
pnpm exec prettier --check \
  build-scripts/gulp/gather-static.js \
  src/habab/native-ui-bridge.ts \
  src/dialogs/config-flow/show-dialog-config-flow.ts \
  src/dialogs/config-flow/show-dialog-options-flow.ts \
  src/dialogs/voice-command-dialog/show-ha-voice-command-dialog.ts \
  src/entrypoints/core.ts \
  src/panels/lovelace/hui-root.ts \
  src/data/preloads.ts \
  src/data/recorder.ts \
  src/layouts/home-assistant.ts \
  test/dialogs/voice-command-dialog/show-ha-voice-command-dialog.test.ts \
  test/habab/native-ui-bridge.test.ts \
  test/data/recorder-info.test.ts
pnpm exec vitest run --config test/vitest.config.ts \
  test/dialogs/voice-command-dialog/show-ha-voice-command-dialog.test.ts \
  test/habab/native-ui-bridge.test.ts \
  test/data/recorder-info.test.ts
pnpm exec gulp gather-gallery-pages
pnpm exec gulp clean
pnpm exec gulp \
  gen-icons-json \
  build-translations \
  build-locale-data \
  gen-licenses

# The exact stock frontend wheel for this Core version supplies the upstream
# translation catalog. The bridge patch does not alter translation keys.
rm -rf build/translations/output
cp -a "$translations" build/translations/output
[[ "$(translation_catalog_sha build/translations/output)" == "$catalog_sha" ]] || {
  echo "Russian translations in build output failed catalog verification" >&2
  exit 1
}
python3 - "$translations" build/translations/translationMetadata.json <<'PY'
import json
import re
import sys
from pathlib import Path

catalog = Path(sys.argv[1])
metadata_path = Path(sys.argv[2])
metadata = json.loads(metadata_path.read_text())
translations = metadata["translations"]
hashes = {}
for path in catalog.glob("*.json"):
    match = re.fullmatch(r"(.+)-([a-f0-9]{32})\.json", path.name)
    if match:
        hashes[match.group(1)] = match.group(2)

if hashes.get("en") != translations.get("en", {}).get("hash"):
    raise SystemExit("Stock English catalog does not match the pinned source build")
if "ru" not in hashes or "ru" not in translations:
    raise SystemExit("Stock Russian catalog or translation metadata is missing")

for language, digest in hashes.items():
    if language in translations:
        translations[language]["hash"] = digest

for language, value in translations.items():
    digest = value.get("hash")
    if not digest:
        continue
    for path in [catalog / f"{language}-{digest}.json"] + [
        catalog / fragment / f"{language}-{digest}.json"
        for fragment in metadata["fragments"]
    ]:
        if not path.is_file():
            raise SystemExit(
                f"Translation metadata points to missing catalog: {path.relative_to(catalog)}"
            )

metadata_path.write_text(json.dumps(metadata, separators=(",", ":")))
PY
pnpm lint:types

# This is the remainder of upstream build-app, with translation metadata
# reconciled before Rspack bundles the metadata into modern and legacy assets.
pnpm exec gulp copy-static-app
python3 "$repo_root/habab/qa/native-font-assets/check-font-assets.py" \
  --static-dir "$worktree/hass_frontend/static"
pnpm exec gulp rspack-prod-app
pnpm exec gulp gen-pages-app-prod gen-service-worker-app-prod
pnpm exec gulp compress-app
pnpm exec gulp prune-compress-cache
pnpm exec gulp gather-gallery-pages
pnpm lint:types

cp pyproject.toml "$pyproject_backup"
python3 - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
text = path.read_text()
old = 'version      = "20260930.4"'
new = 'version      = "20260930.4+habab.4"'
if text.count(old) != 1:
    raise SystemExit("Could not locate the pinned package version exactly once")
path.write_text(text.replace(old, new, 1))
PY
python3 -m build --wheel --outdir "$staging/wheel"
cp "$pyproject_backup" pyproject.toml
rm "$pyproject_backup"

[[ -f "$staging/wheel/$wheel_name" ]] || {
  echo "Build did not produce expected wheel $wheel_name" >&2
  exit 1
}
python3 "$repo_root/habab/qa/native-font-assets/check-font-assets.py" \
  --static-dir "$worktree/hass_frontend/static" \
  --wheel "$staging/wheel/$wheel_name" \
  --report "$repo_root/habab/qa/native-font-assets/font-assets-report.json"
artifact_sha="$(sha256sum "$staging/wheel/$wheel_name" | cut -d' ' -f1)"
artifact_dir="$repo_root/habab/public/downloads/native-frontend/$artifact_sha"
mkdir -p "$artifact_dir"
[[ ! -e "$artifact_dir/$wheel_name" ]] || {
  echo "Immutable artifact already exists; refusing to overwrite it" >&2
  exit 1
}
cp "$staging/wheel/$wheel_name" "$artifact_dir/$wheel_name"
[[ "$(sha256sum "$artifact_dir/$wheel_name" | cut -d' ' -f1)" == "$artifact_sha" ]]
printf 'patch_sha256=%s\ntranslation_catalog_sha256=%s\nwheel_sha256=%s\nwheel_bytes=' "$patch_sha" "$catalog_sha" "$artifact_sha"
wc -c < "$artifact_dir/$wheel_name"
printf 'wheel_path=%s\n' "$artifact_dir/$wheel_name"
