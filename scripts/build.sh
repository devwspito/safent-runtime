#!/bin/sh
# Build the Safent runtime container (Playwright/Ubuntu + systemd PID1).
# NOT a VM image — this produces a Docker/OCI container.
set -eu

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-safent-runtime:clean}"

echo "==> Cleaning stale build artifacts (avoid stale .pyc in the wheel)"
rm -rf build dist src/*.egg-info
find src -name '*.pyc' -delete 2>/dev/null || true

echo "==> Building the wheel (hermes-runtime)"
python3 -m pip wheel . --no-deps -w dist/

echo "==> Building the container image: ${IMAGE}"
podman build -f ops/container/Containerfile -t "${IMAGE}" .

echo ""
echo "==> Done: ${IMAGE}"
echo "    Run: NAME=safent HOST_PORT=17517 IMAGE=${IMAGE} ./ops/container/run-safent.sh"
echo "    (do NOT --cap-drop ALL / no container-wide no-new-privileges — breaks systemd PID1)"
echo "    UI:  http://localhost:17517"
