#!/usr/bin/env bash
# build.sh — build the single Lumen runtime image.
#
# Usage:
#   ./ops/container/build.sh            # build the image
#   ./ops/container/build.sh --push     # build then push (requires registry auth)
#
# There is ONE runtime image. It carries no edition-specific code: pairing state
# at RUNTIME (the .enterprise marker + is_associated()) is what activates the
# cloud-managed associate behavior, so the same image runs both as free community
# and, once paired, as the enterprise associate. (A previous "enterprise" tag was
# functionally identical bar a cosmetic /usr/share/hermes/edition label and has
# been dropped — see the project image-strategy decision.)
#
# DO NOT run builds from CI/CD as a developer — the pipeline owns publishing.
# This script is for local validation only.
set -euo pipefail

RUNTIME="$(command -v podman 2>/dev/null || command -v docker 2>/dev/null || true)"
[ -n "$RUNTIME" ] || { echo "[x] need podman or docker"; exit 1; }

HERE="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root
cd "$HERE"

IMAGE_TAG="${IMAGE_TAG:-ghcr.io/devwspito/lumen:latest}"
FE_CACHEBUST="${FE_CACHEBUST:-$(date +%s)}"
# Bake the git SHA so a local build is identifiable too (CI passes its own github.sha).
GIT_SHA="${GIT_SHA:-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo dev)}"

DO_PUSH=false
for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=true ;;
    *)      echo "[x] unknown flag: $arg"; exit 1 ;;
  esac
done

echo "[*] Building → ${IMAGE_TAG}"
"$RUNTIME" build \
  --build-arg LUMEN_EDITION=community \
  --build-arg FE_CACHEBUST="${FE_CACHEBUST}" \
  --build-arg GIT_SHA="${GIT_SHA}" \
  -f ops/container/Containerfile \
  -t "${IMAGE_TAG}" .
echo "[ok] ${IMAGE_TAG} built"

if [ "$DO_PUSH" = true ]; then
  echo "[*] Pushing ${IMAGE_TAG}..."
  "$RUNTIME" push "${IMAGE_TAG}"
  echo "[ok] ${IMAGE_TAG} pushed"
fi
