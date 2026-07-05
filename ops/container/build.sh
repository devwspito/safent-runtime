#!/usr/bin/env bash
# build.sh — the ONE way to build (and optionally push) the Safent runtime image.
#
# Usage:
#   ./ops/container/build.sh            # sync version, build wheel + image
#   ./ops/container/build.sh --push     # …then push :<VERSION> and :latest
#
# SINGLE SOURCE OF VERSION TRUTH: the repo-root `VERSION` file. This script syncs it
# into pyproject.toml (the wheel) and src/hermes/__init__.py (runtime __version__),
# builds a SINGLE fresh wheel (dist/ is wiped first so no stale wheel can be picked
# by the Containerfile glob), builds the image tagged BOTH :<VERSION> and :latest,
# and VERIFIES the baked image reports that exact version. To ship a new version:
# edit VERSION, run this, then --push. Do not hand-tag or hand-bump anything else.
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
FE_CACHEBUST="${FE_CACHEBUST:-$(date +%s)}"
GIT_SHA="${GIT_SHA:-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo dev)}"

DO_PUSH=false
for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=true ;;
    *)      echo "[x] unknown flag: $arg"; exit 1 ;;
  esac
done

echo "[*] Version (from VERSION file): ${VERSION}"

# 1) Sync the single version into the wheel + runtime (idempotent).
sed -i -E "s/^version = \".*\"/version = \"${VERSION}\"/" pyproject.toml
# Match the WHOLE line so an existing trailing comment is replaced, not duplicated.
sed -i -E "s|^__version__ = .*|__version__ = \"${VERSION}\"  # single source of truth: repo-root VERSION (synced by build.sh)|" src/hermes/__init__.py
echo "[ok] synced pyproject.toml + src/hermes/__init__.py → ${VERSION}"

# 2) Build a SINGLE fresh wheel (wipe dist/ so the Containerfile glob is unambiguous).
rm -f dist/*.whl
python3 -m pip wheel . --no-deps -w dist/ >/dev/null
WHEELS=(dist/*.whl)
[ "${#WHEELS[@]}" -eq 1 ] || { echo "[x] expected exactly 1 wheel in dist/, found ${#WHEELS[@]}"; exit 1; }
echo "[ok] wheel: ${WHEELS[0]}"

# 3) Build the image, tagged BOTH :<VERSION> and :latest.
echo "[*] Building → ${IMAGE_VERSIONED} (+ :latest)"
"$RUNTIME" build \
  --build-arg SAFENT_EDITION=community \
  --build-arg SAFENT_VERSION="${VERSION}" \
  --build-arg FE_CACHEBUST="${FE_CACHEBUST}" \
  --build-arg APP_CACHEBUST="${FE_CACHEBUST}" \
  --build-arg GIT_SHA="${GIT_SHA}" \
  -f ops/container/Containerfile \
  -t "${IMAGE_VERSIONED}" \
  -t "${IMAGE_LATEST}" .

# 4) VERIFY the baked image reports the exact version (catches stale-wheel bugs).
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
