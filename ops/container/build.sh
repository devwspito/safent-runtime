#!/usr/bin/env bash
# build.sh — build the CE and/or EE image tags from the SAME Containerfile.
#
# Usage:
#   ./ops/container/build.sh            # builds CE tag (default)
#   ./ops/container/build.sh --ee       # builds EE tag
#   ./ops/container/build.sh --all      # builds both CE and EE
#   ./ops/container/build.sh --push     # push after building (requires registry auth)
#
# The Containerfile accepts ARG LUMEN_EDITION=community (default) | enterprise.
# The two tags are FUNCTIONALLY IDENTICAL images: the ONLY build-time difference
# is the cosmetic label string written to /usr/share/hermes/edition (surfaced on
# /api/v1/profile, read by nothing). hermes-config-sync is enabled in BOTH
# editions; what actually makes it run is RUNTIME pairing state (the .enterprise
# marker + is_associated()), not the build. The EE build reuses the CE layer
# cache and is fast — and the EE tag is effectively redundant (a paired CE image
# already runs in associate mode).
#
# DO NOT run builds from CI/CD as a developer — the pipeline owns publishing.
# This script is for local validation only.
set -euo pipefail

RUNTIME="$(command -v podman 2>/dev/null || command -v docker 2>/dev/null || true)"
[ -n "$RUNTIME" ] || { echo "[x] need podman or docker"; exit 1; }

HERE="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root
cd "$HERE"

CE_TAG="${CE_TAG:-ghcr.io/devwspito/lumen:latest}"
EE_TAG="${EE_TAG:-ghcr.io/devwspito/lumen-enterprise:latest}"
FE_CACHEBUST="${FE_CACHEBUST:-$(date +%s)}"

BUILD_CE=false
BUILD_EE=false
DO_PUSH=false

for arg in "$@"; do
  case "$arg" in
    --ee)   BUILD_EE=true ;;
    --all)  BUILD_CE=true; BUILD_EE=true ;;
    --push) DO_PUSH=true ;;
    *)      echo "[x] unknown flag: $arg"; exit 1 ;;
  esac
done

# Default: CE only
[ "$BUILD_CE" = false ] && [ "$BUILD_EE" = false ] && BUILD_CE=true

_build() {
  local edition="$1" tag="$2"
  echo "[*] Building ${edition} → ${tag}"
  "$RUNTIME" build \
    --build-arg LUMEN_EDITION="${edition}" \
    --build-arg FE_CACHEBUST="${FE_CACHEBUST}" \
    -f ops/container/Containerfile \
    -t "${tag}" .
  echo "[ok] ${tag} built"
  if [ "$DO_PUSH" = true ]; then
    echo "[*] Pushing ${tag}..."
    "$RUNTIME" push "${tag}"
    echo "[ok] ${tag} pushed"
  fi
}

[ "$BUILD_CE" = true ] && _build community "$CE_TAG"
[ "$BUILD_EE" = true ] && _build enterprise "$EE_TAG"
