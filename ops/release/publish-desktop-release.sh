#!/usr/bin/env bash
# publish-desktop-release.sh — publish a Safent desktop GitHub Release from the
# artifacts of a safent-desktop.yml run (devwspito/agents-autonomy) onto
# devwspito/safent-runtime. Runs on this DGX, never in CI. See README.md.
#
# Usage: publish-desktop-release.sh <run-id> <tag> [--dry-run] [--notes-file FILE]
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

RUN_REPO="devwspito/agents-autonomy"
RELEASE_REPO="devwspito/safent-runtime"
ASSET_BASE_URL="https://github.com/${RELEASE_REPO}/releases/download"

# Test-only overrides (mirrors hermes.shell_server.runtime_manifest's own
# SAFENT_RUNTIME_MANIFEST_PUBKEY convention: production always reads the
# committed file / real 2 GiB limit; only tests point elsewhere).
SIZE_LIMIT_BYTES="${PUBLISH_RELEASE_SIZE_LIMIT_BYTES:-$((2 * 1024 * 1024 * 1024))}"
PUBKEY_FILE="${RUNTIME_MANIFEST_PUBKEY_FILE:-${REPO_ROOT}/ops/keys/runtime-manifest.pub}"

DRY_RUN=0
RUN_ID=""
TAG=""
NOTES_FILE=""

WORKDIR=""
ASSET_FILES=()
DMG_FILE=""
LATEST_JSON=""
RUNTIME_MANIFEST_JSON=""
RUNTIME_MANIFEST_SIG=""
CHECKSUM_MANIFEST=""
TRUNK_COMMIT=""
EXISTING_RELEASE=0
EXISTING_IS_DRAFT="false"

log()  { printf '[publish-desktop-release] %s\n' "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }

usage() {
  cat >&2 <<'EOF'
Usage: publish-desktop-release.sh <run-id> <tag> [--dry-run] [--notes-file FILE]

  <run-id>       GitHub Actions run id of safent-desktop.yml (devwspito/agents-autonomy)
  <tag>          Release tag to publish on devwspito/safent-runtime, e.g. v0.9.0
  --dry-run      Verify everything and print the plan; never touches GitHub
  --notes-file   Path to a release-notes markdown file (default: a one-line stub)
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --notes-file)
        [[ $# -ge 2 ]] || die "--notes-file requires a path"
        NOTES_FILE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      -*) die "unknown flag: $1" ;;
      *)
        if [[ -z "$RUN_ID" ]]; then RUN_ID="$1"
        elif [[ -z "$TAG" ]]; then TAG="$1"
        else die "unexpected argument: $1"
        fi
        shift ;;
    esac
  done
  if [[ -z "$RUN_ID" || -z "$TAG" ]]; then
    usage
    exit 1
  fi
  [[ "$TAG" =~ ^v[0-9] ]] || die "tag must look like vX.Y.Z (got: $TAG)"
}

require_tools() {
  local tool
  for tool in gh jq python3 git sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool not on PATH: $tool"
  done
  [[ -f "$PUBKEY_FILE" ]] || die "runtime manifest public key not found: $PUBKEY_FILE"
}

file_size() {
  stat -c%s "$1" 2>/dev/null || stat -f%z "$1"
}

# ---------------------------------------------------------------------------
# Acquire + locate the run's artifacts
# ---------------------------------------------------------------------------

download_artifacts() {
  local artifacts_dir="$WORKDIR/artifacts"
  mkdir -p "$artifacts_dir"
  log "Downloading artifacts for run $RUN_ID ($RUN_REPO)…"
  gh run download "$RUN_ID" --repo "$RUN_REPO" --dir "$artifacts_dir" \
    || die "gh run download failed for run $RUN_ID — does it exist and have artifacts?"
  printf '%s' "$artifacts_dir"
}

find_first() {
  # find_first <dir> <find-name-flag e.g. -iname> <pattern> — empty if none found.
  find "$1" -type f "$2" "$3" -print -quit 2>/dev/null || true
}

collect_assets() {
  local artifacts_dir="$1"

  DMG_FILE="$(find_first "$artifacts_dir" -iname '*.dmg')"
  LATEST_JSON="$(find_first "$artifacts_dir" -iname 'latest.json')"
  RUNTIME_MANIFEST_JSON="$(find_first "$artifacts_dir" -iname 'runtime-manifest.json')"
  RUNTIME_MANIFEST_SIG="$(find_first "$artifacts_dir" -iname 'runtime-manifest.json.minisig')"
  CHECKSUM_MANIFEST="$(find_first "$artifacts_dir" -iname 'SHA256SUMS')"

  local candidates=()
  mapfile -t candidates < <(
    find "$artifacts_dir" -type f \
      \( -iname '*.dmg' -o -iname '*.deb' -o -iname '*.rpm' -o -iname '*.sig' \
         -o -iname 'latest.json' -o -iname 'runtime-manifest.json' \
         -o -iname 'runtime-manifest.json.minisig' \) \
      | sort
  )
  dedupe_assets "${candidates[@]}"
}

# Same basename appearing twice with DIFFERENT content is a packaging bug we
# must not silently paper over by uploading whichever one `find` saw last.
dedupe_assets() {
  local f base hash
  declare -A seen_hash=()
  declare -A kept_path=()
  for f in "$@"; do
    base="$(basename "$f")"
    hash="$(sha256sum "$f" | awk '{print $1}')"
    if [[ -n "${seen_hash[$base]:-}" ]]; then
      [[ "${seen_hash[$base]}" == "$hash" ]] \
        || die "duplicate asset name with different content: $base"
      continue
    fi
    seen_hash[$base]="$hash"
    kept_path[$base]="$f"
  done
  ASSET_FILES=()
  for base in "${!kept_path[@]}"; do
    ASSET_FILES+=("${kept_path[$base]}")
  done
  mapfile -t ASSET_FILES < <(printf '%s\n' "${ASSET_FILES[@]}" | sort)
}

# ---------------------------------------------------------------------------
# Verification gates — every one is fail-closed: absence is failure, not skip.
# ---------------------------------------------------------------------------

verify_dmg_present() {
  [[ -n "$DMG_FILE" ]] || die "no .dmg found among run artifacts — macOS is mandatory for a release"
}

verify_checksum_manifest_present() {
  [[ -n "$CHECKSUM_MANIFEST" ]] \
    || die "no SHA256SUMS manifest found among run artifacts — refusing to publish without an integrity source"
}

verify_checksums() {
  local f base expected actual
  for f in "${ASSET_FILES[@]}"; do
    base="$(basename "$f")"
    expected="$(awk -v b="$base" '{n=$2; sub(/^\*/, "", n); if (n == b) {print $1; exit}}' "$CHECKSUM_MANIFEST")"
    [[ -n "$expected" ]] || die "no sha256 entry for $base in $(basename "$CHECKSUM_MANIFEST")"
    actual="$(sha256sum "$f" | awk '{print $1}')"
    [[ "$expected" == "$actual" ]] \
      || die "sha256 mismatch for $base: manifest=$expected actual=$actual"
  done
}

verify_sizes() {
  local f size
  for f in "${ASSET_FILES[@]}"; do
    size="$(file_size "$f")"
    (( size < SIZE_LIMIT_BYTES )) \
      || die "$(basename "$f") is $size bytes — exceeds the 2 GiB GitHub Releases limit"
  done
}

verify_latest_json_version() {
  jq -e . "$LATEST_JSON" >/dev/null 2>&1 || die "latest.json is not valid JSON"
  local version
  version="$(jq -r '.version' "$LATEST_JSON")"
  [[ "v${version}" == "$TAG" ]] \
    || die "tag $TAG does not match latest.json version v${version}"
}

verify_platform_entries() {
  jq -e '.platforms' "$LATEST_JSON" >/dev/null 2>&1 || die "latest.json has no .platforms object"

  local artifacts_dir="$1" platform url signature fname expected_url asset_path sig_path sig_content sig_expected
  while IFS= read -r platform; do
    url="$(jq -r --arg p "$platform" '.platforms[$p].url' "$LATEST_JSON")"
    signature="$(jq -r --arg p "$platform" '.platforms[$p].signature' "$LATEST_JSON")"
    fname="$(basename "$url")"
    expected_url="${ASSET_BASE_URL}/${TAG}/${fname}"
    [[ "$url" == "$expected_url" ]] \
      || die "platforms.$platform.url mismatch: got '$url', want '$expected_url'"

    asset_path="$(find "$artifacts_dir" -type f -name "$fname" -print -quit 2>/dev/null || true)"
    [[ -n "$asset_path" ]] \
      || die "platforms.$platform references $fname but no such file is present among run artifacts"

    sig_path="$(find "$artifacts_dir" -type f -name "${fname}.sig" -print -quit 2>/dev/null || true)"
    [[ -n "$sig_path" ]] || die "no .sig sidecar found for $fname (platforms.$platform)"
    sig_content="$(tr -d '[:space:]' < "$sig_path")"
    sig_expected="$(printf '%s' "$signature" | tr -d '[:space:]')"
    [[ "$sig_content" == "$sig_expected" ]] \
      || die "$fname.sig content does not match platforms.$platform.signature"
  done < <(jq -r '.platforms | keys[]' "$LATEST_JSON")
}

verify_runtime_manifest_signature() {
  [[ -n "$RUNTIME_MANIFEST_JSON" ]] \
    || die "runtime-manifest.json not found among run artifacts (contracts/update.md §7 gate)"
  [[ -n "$RUNTIME_MANIFEST_SIG" ]] \
    || die "runtime-manifest.json.minisig not found among run artifacts"

  if ! PYTHONPATH="${REPO_ROOT}/src" python3 - "$RUNTIME_MANIFEST_JSON" "$PUBKEY_FILE" "$RUNTIME_MANIFEST_SIG" <<'PYEOF'
import sys
from hermes.shell_server.runtime_manifest import verify_minisign

manifest_path, pubkey_path, minisig_path = sys.argv[1:4]
file_bytes = open(manifest_path, "rb").read()
pubkey_text = open(pubkey_path, encoding="utf-8").read()
minisig_text = open(minisig_path, encoding="utf-8").read()
sys.exit(0 if verify_minisign(file_bytes, pubkey_text, minisig_text) else 1)
PYEOF
  then
    die "runtime-manifest.json.minisig does not verify against $(basename "$PUBKEY_FILE")"
  fi
}

# "the trunk commit being released": this script's own checkout IS
# devwspito/safent-runtime (see README) — its current HEAD is the commit the
# release is targeted at. If the tag already exists (pushed by a previous
# partial run, say), it must point at that same commit — never silently
# publish against a tag that has drifted onto a different commit.
verify_tag_commit() {
  git -C "$REPO_ROOT" fetch origin --tags --quiet 2>/dev/null \
    || log "warning: git fetch --tags failed (offline?) — using local refs only"
  TRUNK_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"

  if git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    git -C "$REPO_ROOT" tag --points-at "$TRUNK_COMMIT" | grep -qx "$TAG" \
      || die "tag $TAG already exists but does not point at $TRUNK_COMMIT (the commit being released)"
  fi
}

# ---------------------------------------------------------------------------
# Plan + publish
# ---------------------------------------------------------------------------

print_plan() {
  local f
  log "Plan for $TAG on $RELEASE_REPO (target ${TRUNK_COMMIT}):"
  for f in "${ASSET_FILES[@]}"; do
    log "  upload: $(basename "$f")  ($(file_size "$f") bytes)"
  done
}

resolve_notes_file() {
  if [[ -z "$NOTES_FILE" ]]; then
    NOTES_FILE="$WORKDIR/notes.md"
    printf 'Safent %s.\n' "$TAG" > "$NOTES_FILE"
    return
  fi
  [[ -f "$NOTES_FILE" ]] || die "--notes-file not found: $NOTES_FILE"
  NOTES_FILE="$(cd -- "$(dirname -- "$NOTES_FILE")" && pwd)/$(basename -- "$NOTES_FILE")"
}

# Sets EXISTING_RELEASE / EXISTING_IS_DRAFT. Read-only — safe under --dry-run.
inspect_existing_release() {
  local json
  if json="$(gh release view "$TAG" --repo "$RELEASE_REPO" --json isDraft 2>/dev/null)"; then
    EXISTING_RELEASE=1
    EXISTING_IS_DRAFT="$(jq -r '.isDraft' <<<"$json")"
  else
    EXISTING_RELEASE=0
    EXISTING_IS_DRAFT="false"
  fi
}

publish_release() {
  if (( EXISTING_RELEASE )) && [[ "$EXISTING_IS_DRAFT" == "false" ]]; then
    log "Release $TAG already published on $RELEASE_REPO — nothing to do (idempotent)."
    return 0
  fi

  if (( EXISTING_RELEASE )); then
    log "Reusing existing draft release $TAG on $RELEASE_REPO."
  else
    log "Creating draft release $TAG on $RELEASE_REPO…"
    gh release create "$TAG" --repo "$RELEASE_REPO" --target "$TRUNK_COMMIT" \
      --draft --title "Safent ${TAG}" --notes-file "$NOTES_FILE"
  fi

  log "Uploading ${#ASSET_FILES[@]} asset(s)…"
  if gh release upload "$TAG" "${ASSET_FILES[@]}" --repo "$RELEASE_REPO" --clobber; then
    gh release edit "$TAG" --repo "$RELEASE_REPO" --draft=false --latest
    log "Published $TAG on $RELEASE_REPO (undrafted, marked latest)."
  else
    die "asset upload failed — leaving $TAG as a draft for manual inspection; re-run to retry (idempotent)"
  fi
}

main() {
  parse_args "$@"
  require_tools

  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/publish-desktop-release.XXXXXX")"
  trap 'rm -rf "$WORKDIR"' EXIT

  local artifacts_dir
  artifacts_dir="$(download_artifacts)"
  collect_assets "$artifacts_dir"

  verify_dmg_present
  verify_checksum_manifest_present
  verify_checksums
  verify_sizes
  verify_latest_json_version
  verify_platform_entries "$artifacts_dir"
  verify_runtime_manifest_signature
  verify_tag_commit

  print_plan
  resolve_notes_file
  inspect_existing_release

  if (( DRY_RUN )); then
    if (( EXISTING_RELEASE )); then
      log "Existing release found (isDraft=$EXISTING_IS_DRAFT) — dry run would $([[ "$EXISTING_IS_DRAFT" == "true" ]] && echo "reuse it" || echo "no-op, already published")."
    else
      log "No existing release for $TAG — dry run would create a new draft."
    fi
    log "Dry run: all checks passed. No changes made."
    exit 0
  fi

  publish_release
}

main "$@"
