#!/usr/bin/env bash
# build.sh — the ONE way to build (and optionally push) the Safent runtime image.
#
# Usage:
#   ./ops/container/build.sh            # validate version, build image
#   ./ops/container/build.sh --push     # …then push :<VERSION> and :latest
#
# SINGLE SOURCE OF VERSION TRUTH: the repo-root `VERSION` file. This script
# validates pyproject.toml, builds the image tagged BOTH :<VERSION> and :latest,
# and VERIFIES the baked image reports that exact version. The wheel is built once,
# inside the Containerfile; this wrapper never builds a redundant host wheel.
#
# There is ONE runtime image (community == enterprise; pairing at runtime activates
# associate behavior). DO NOT build/push from CI as a developer — the pipeline owns
# publishing; this is for local validation + the owner's manual publish.
set -euo pipefail

RUNTIME="$(command -v podman 2>/dev/null || command -v docker 2>/dev/null || true)"
[ -n "$RUNTIME" ] || { echo "[x] need podman or docker"; exit 1; }

HERE="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root
cd "$HERE"

[ -f VERSION ] || { echo "[x] VERSION file missing at repo root"; exit 1; }
VERSION="$(tr -d ' \t\n\r' < VERSION)"
[ -n "$VERSION" ] || { echo "[x] VERSION file is empty"; exit 1; }

REGISTRY="${REGISTRY:-ghcr.io/devwspito/safent}"
IMAGE_VERSIONED="${REGISTRY}:${VERSION}"
IMAGE_LATEST="${REGISTRY}:latest"
GIT_SHA="${GIT_SHA:-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo dev)}"

DO_PUSH=false
for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=true ;;
    *)      echo "[x] unknown flag: $arg"; exit 1 ;;
  esac
done

echo "[*] Version (from VERSION file): ${VERSION}"

# 1) Source metadata must already agree; builds never rewrite the checkout.
PYPROJECT_VERSION="$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -1)"
[ "$PYPROJECT_VERSION" = "$VERSION" ] || {
  echo "[x] pyproject.toml version '${PYPROJECT_VERSION}' differs from VERSION '${VERSION}'"
  exit 1
}

# 2) Build the image, tagged BOTH :<VERSION> and :latest. Content-addressed
# layers stay reusable; use the engine's explicit --no-cache only for diagnosis.
echo "[*] Building → ${IMAGE_VERSIONED} (+ :latest)"
BUILD_FORMAT_ARGS=()
# Podman defaults to OCI, whose image format discards Dockerfile HEALTHCHECK.
# The delivered image is also consumed by Docker/Compose, so retain that metadata.
if [ "$(basename "$RUNTIME")" = "podman" ]; then
  BUILD_FORMAT_ARGS+=(--format docker)
fi
"$RUNTIME" build "${BUILD_FORMAT_ARGS[@]}" \
  --build-arg SAFENT_EDITION=community \
  --build-arg GIT_SHA="${GIT_SHA}" \
  -f ops/container/Containerfile \
  -t "${IMAGE_VERSIONED}" \
  -t "${IMAGE_LATEST}" .

# 3) VERIFY the baked image reports the exact version (catches stale-wheel bugs).
BAKED="$("$RUNTIME" run --rm --entrypoint python3 "${IMAGE_VERSIONED}" -c 'import hermes; print(hermes.__version__)' 2>/dev/null | tr -d ' \t\n\r')"
if [ "$BAKED" != "$VERSION" ]; then
  echo "[x] VERIFY FAILED: image reports hermes.__version__='${BAKED}', expected '${VERSION}'"
  exit 1
fi
echo "[ok] verified baked image reports ${BAKED}"
echo "[ok] built ${IMAGE_VERSIONED} + ${IMAGE_LATEST}"

if [ "$DO_PUSH" = true ]; then
  for tag in "${IMAGE_VERSIONED}" "${IMAGE_LATEST}"; do
    echo "[*] Pushing ${tag}..."
    "$RUNTIME" push "${tag}"
    echo "[ok] pushed ${tag}"
  done
else
  echo
  echo "To publish (owner):"
  echo "  ${RUNTIME} push ${IMAGE_VERSIONED}"
  echo "  ${RUNTIME} push ${IMAGE_LATEST}"
fi
