#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
worktree="${HABAB_NATIVE_FRONTEND_WORKTREE:-/root/.local/share/habab-native-frontend9-build}"
translations="${HABAB_NATIVE_FRONTEND_TRANSLATIONS:-/root/.local/share/habab-native-frontend-translations-official/translations}"
source_commit="26690e06b0bf64a1487929245c32842ef92c62b5"
patch_file="$repo_root/habab/native-frontend/bridge.patch"
patch_sha="d483686b341c34705a62ca98f7358023599bdb92c80b039b005d4eeef472e092"
catalog_sha="154cc8ea8822c85d63b46e34ccc667cbac01df2cbf8cacb0a300bfe1fecda3ce"
wheel_version="20260930.4+habab.9"
artifact_root="${HABAB_NATIVE_FRONTEND_ARTIFACT_ROOT:-$repo_root/habab/public/downloads/native-frontend}"
retention_tool="${RELEASE_RETENTION_TOOL:-/root/tools/release-retention/retention.py}"
retention_state="${HABAB_NATIVE_FRONTEND_RETENTION_STATE:-/root/tools/release-retention/states/habab-native-frontend.json}"
wheel_name="home_assistant_frontend-${wheel_version}-py3-none-any.whl"
staging="$(mktemp -d /root/.local/share/habab-native-frontend-wheel.XXXXXX)"
pyproject_backup="$staging/pyproject.toml"
wheel_tmp=""

cleanup() {
  if [[ -f "$pyproject_backup" ]]; then
    cp "$pyproject_backup" "$worktree/pyproject.toml"
  fi
  if [[ -n "$wheel_tmp" && -e "$wheel_tmp" ]]; then
    rm -f "$wheel_tmp"
  fi
  rm -rf "$staging"
}
trap cleanup EXIT

[[ "$wheel_version" =~ ^20260930\.4\+habab\.[0-9]+$ ]] || {
  echo "Wheel version must match the pinned Core version and Habab release suffix" >&2
  exit 1
}
[[ "$artifact_root" == /* && "$retention_tool" == /* && "$retention_state" == /* ]] || {
  echo "Artifact root and retention paths must be absolute" >&2
  exit 1
}
retention_release="${wheel_version#*+}"
retention_skip_reason=""
if [[ ! -f "$retention_tool" ]]; then
  retention_skip_reason="retention tool is absent: $retention_tool"
elif [[ ! -f "$retention_state" ]]; then
  retention_skip_reason="retention state is absent: $retention_state"
else
  if ! retention_skip_reason="$(python3 - "$retention_state" "$artifact_root" <<'PY'
import json
from pathlib import Path
import sys

state_path = Path(sys.argv[1])
artifact_root = Path(sys.argv[2]).resolve()
try:
    data = json.loads(state_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    print("retention state cannot be read")
    raise SystemExit(1)
if not isinstance(data, dict):
    print("retention state must be a JSON object")
    raise SystemExit(1)

root_value = data.get("root")
root = Path(root_value) if isinstance(root_value, str) else Path()
if data.get("schema") != 1 or not root.is_absolute() or root.resolve() != root or not root.is_dir():
    print("retention state has no valid canonical root")
    raise SystemExit(1)
if root != artifact_root:
    print(f"retention root does not match artifact root: {root}")
    raise SystemExit(1)

current, previous = data.get("current"), data.get("previous")
releases = data.get("releases")
if (not current or not previous or current == previous or not isinstance(releases, dict)
        or current not in releases or previous not in releases):
    print("retention current/previous pointers are not initialized")
    raise SystemExit(1)
PY
)"; then
    :
  fi
fi

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
export SKIP_FETCH_NIGHTLY_TRANSLATIONS=1
cd "$worktree"
corepack enable pnpm
[[ "$(pnpm --version)" == "11.28.0" ]] || {
  echo "Unexpected pnpm version" >&2
  exit 1
}

pnpm install --frozen-lockfile
pnpm exec eslint \
  src/panels/home/ha-panel-home.ts \
  test/panels/home/ha-panel-home.test.ts \
  src/panels/lovelace/strategies/usage_prediction/common-controls-section-strategy.ts \
  src/panels/lovelace/strategies/helpers/favorite-cards.ts \
  test/panels/lovelace/strategies/common-controls-section-strategy.test.ts \
  build-scripts/gulp/gather-static.js \
  src/habab/native-ui-bridge.ts \
  src/data/context/dirty-state.ts \
  src/mixins/dirty-state-provider-mixin.ts \
  src/dialogs/config-flow/dialog-data-entry-flow.ts \
  src/util/register-service-worker.ts \
  src/dialogs/config-flow/show-dialog-config-flow.ts \
  src/dialogs/config-flow/show-dialog-options-flow.ts \
  src/dialogs/voice-command-dialog/show-ha-voice-command-dialog.ts \
  test/build-scripts/rspack-config.test.js \
  build-scripts/rspack.cjs \
  build-scripts/gulp/rspack.js \
  src/entrypoints/app.ts \
  src/entrypoints/core.ts \
  src/panels/lovelace/hui-root.ts \
  src/data/preloads.ts \
  src/data/recorder.ts \
  src/layouts/home-assistant.ts \
  test/mixins/dirty-state-provider-mixin.test.ts \
  test/util/register-service-worker.test.ts \
  test/data/recorder-info.test.ts \
  --max-warnings=0
pnpm exec prettier --check \
  src/panels/home/ha-panel-home.ts \
  test/panels/home/ha-panel-home.test.ts \
  src/panels/lovelace/strategies/usage_prediction/common-controls-section-strategy.ts \
  src/panels/lovelace/strategies/helpers/favorite-cards.ts \
  test/panels/lovelace/strategies/common-controls-section-strategy.test.ts \
  build-scripts/gulp/gather-static.js \
  src/habab/native-ui-bridge.ts \
  src/data/context/dirty-state.ts \
  src/mixins/dirty-state-provider-mixin.ts \
  src/dialogs/config-flow/dialog-data-entry-flow.ts \
  src/util/register-service-worker.ts \
  src/dialogs/config-flow/show-dialog-config-flow.ts \
  src/dialogs/config-flow/show-dialog-options-flow.ts \
  src/dialogs/voice-command-dialog/show-ha-voice-command-dialog.ts \
  build-scripts/rspack.cjs \
  build-scripts/gulp/rspack.js \
  src/entrypoints/app.ts \
  src/entrypoints/core.ts \
  src/panels/lovelace/hui-root.ts \
  src/data/preloads.ts \
  src/data/recorder.ts \
  src/layouts/home-assistant.ts \
  test/mixins/dirty-state-provider-mixin.test.ts \
  test/util/register-service-worker.test.ts \
  test/dialogs/voice-command-dialog/show-ha-voice-command-dialog.test.ts \
  test/habab/native-ui-bridge.test.ts \
  test/build-scripts/rspack-config.test.js \
  test/data/recorder-info.test.ts
# Vitest's import graph includes generated translation metadata. Prepare it
# from the pinned source catalog before tests, without fetching nightly files.
pnpm exec gulp build-translations
pnpm exec vitest run --config test/vitest.config.ts \
  --maxWorkers=1 \
  --no-file-parallelism \
  test/panels/home/ha-panel-home.test.ts \
  test/panels/lovelace/strategies/common-controls-section-strategy.test.ts \
  test/build-scripts/rspack-config.test.js \
  test/dialogs/voice-command-dialog/show-ha-voice-command-dialog.test.ts \
  test/habab/native-ui-bridge.test.ts \
  test/mixins/dirty-state-provider-mixin.test.ts \
  test/util/register-service-worker.test.ts \
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
HABAB_TERSER_WORKERS=1 pnpm exec gulp rspack-prod-app-modern
HABAB_TERSER_WORKERS=1 pnpm exec gulp rspack-prod-app-legacy
pnpm exec gulp gen-pages-app-prod gen-service-worker-app-prod
pnpm exec gulp compress-app
pnpm exec gulp prune-compress-cache
pnpm exec gulp gather-gallery-pages
pnpm lint:types

cp pyproject.toml "$pyproject_backup"
python3 - "$wheel_version" <<'PY'
from pathlib import Path
import sys

path = Path("pyproject.toml")
text = path.read_text()
old = 'version      = "20260930.4"'
new = f'version      = "{sys.argv[1]}"'
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
artifact_dir="$artifact_root/$artifact_sha"
mkdir -p "$artifact_dir"
mkdir -p "$artifact_root"
[[ ! -L "$artifact_dir" ]] || {
  echo "Artifact hash directory must not be a symlink" >&2
  exit 1
}
if [[ -e "$artifact_dir" ]]; then
  [[ -d "$artifact_dir" ]] || {
    echo "Artifact hash path is not a directory" >&2
    exit 1
  }
else
  mkdir "$artifact_dir"
fi
artifact_root_real="$(realpath -e "$artifact_root")"
artifact_dir_real="$(realpath -e "$artifact_dir")"
[[ "$artifact_dir_real" == "$artifact_root_real/$artifact_sha" ]] || {
  echo "Artifact hash directory escaped the configured artifact root" >&2
  exit 1
}
if [[ -e "$artifact_dir/$wheel_name" || -L "$artifact_dir/$wheel_name" ]]; then
  [[ -f "$artifact_dir/$wheel_name" && ! -L "$artifact_dir/$wheel_name" && \
    "$(sha256sum "$artifact_dir/$wheel_name" | cut -d' ' -f1)" == "$artifact_sha" ]] || {
    echo "Immutable artifact already exists with different or invalid contents" >&2
    exit 1
  }
else
  wheel_tmp="$(mktemp "$artifact_dir/.$wheel_name.tmp.XXXXXX")"
  cp "$staging/wheel/$wheel_name" "$wheel_tmp"
  [[ "$(sha256sum "$wheel_tmp" | cut -d' ' -f1)" == "$artifact_sha" ]]
  mv "$wheel_tmp" "$artifact_dir/$wheel_name"
  wheel_tmp=""
fi
[[ "$(sha256sum "$artifact_dir/$wheel_name" | cut -d' ' -f1)" == "$artifact_sha" ]]
if [[ -n "$retention_skip_reason" ]]; then
  printf 'release retention SKIP: %s\n' "$retention_skip_reason" >&2
else
  python3 "$retention_tool" --state "$retention_state" \
    --release "$retention_release" --artifact "$artifact_sha" --apply
  printf 'release retention applied: %s (%s)\n' "$retention_release" "$artifact_sha"
fi
printf 'patch_sha256=%s\ntranslation_catalog_sha256=%s\nwheel_sha256=%s\nwheel_bytes=' "$patch_sha" "$catalog_sha" "$artifact_sha"
wc -c < "$artifact_dir/$wheel_name"
printf 'wheel_path=%s\n' "$artifact_dir/$wheel_name"
