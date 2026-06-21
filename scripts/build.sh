#!/bin/sh
# Build the Lumen runtime container (Playwright/Ubuntu + systemd PID1).
# NOT a VM image — this produces a Docker/OCI container.
set -eu

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-lumen-runtime:clean}"

echo "==> Cleaning stale build artifacts (avoid stale .pyc in the wheel)"
rm -rf build dist src/*.egg-info
find src -name '*.pyc' -delete 2>/dev/null || true

echo "==> Building the wheel (hermes-runtime)"
python3 -m pip wheel . --no-deps -w dist/

echo "==> Building the container image: ${IMAGE}"
podman build -f ops/container/Containerfile -t "${IMAGE}" .

echo ""
echo "==> Done: ${IMAGE}"
echo "    Run: podman run -d --name lumen --systemd=always \\"
echo "           --cap-drop ALL --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \\"
echo "           --security-opt no-new-privileges --shm-size=1g -p 17517:7517 ${IMAGE}"
echo "    UI:  http://localhost:17517"
