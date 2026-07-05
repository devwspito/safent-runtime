#!/usr/bin/env bash
# install.sh — instalador simple de Safent: limpia, construye y arranca en un comando.
#
# Filosofía: el instalador SOLO levanta Safent. Todo lo "custom" (modelo/proveedor,
# agentes, MCP, skills, integraciones) se configura en la UI.
# Para la instalación de usuario final, usa el CLI: `npx @devwspito/safent`.
# Este script es el camino DESDE FUENTE (construye la imagen localmente).
#
#   ./ops/container/install.sh [PUERTO]      (puerto por defecto: 17517)
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")/../.." && pwd)"   # raíz del repo
cd "$HERE"

IMAGE="safent-runtime:clean"
PORT="${1:-17517}"
NAME="${SAFENT_NAME:-safent}"
RUNTIME="$(command -v podman || command -v docker || true)"
[ -n "$RUNTIME" ] || { echo "✗ necesitas podman o docker instalado"; exit 1; }

# La jaula (Landlock/netns) necesita una máquina rootful en macOS.
if [ "$(basename "$RUNTIME")" = podman ]; then
  rootful="$(podman machine inspect --format '{{.Rootful}}' 2>/dev/null || echo unknown)"
  if [ "$rootful" = "false" ]; then
    echo "✗ la 'podman machine' es rootless; la jaula necesita rootful:"
    echo "    podman machine stop && podman machine set --rootful && podman machine start"
    exit 1
  fi
fi

echo "▸ 1/4 Limpieza (estado de fábrica)…"
"$RUNTIME" rm -f "$NAME" >/dev/null 2>&1 || true
"$RUNTIME" volume rm safent-data >/dev/null 2>&1 || true
"$RUNTIME" builder prune -af >/dev/null 2>&1 || true
"$RUNTIME" image prune -af   >/dev/null 2>&1 || true

echo "▸ 2/4 Construyendo imagen (wheel py3.12 + frontend React, dentro del contenedor)…"
"$RUNTIME" build --build-arg FE_CACHEBUST="$(date +%s)" \
  -f ops/container/Containerfile -t "$IMAGE" .

echo "▸ 3/4 Arrancando (launcher canónico endurecido)…"
SAFENT_NAME="$NAME" ./ops/container/run-safent.sh "$IMAGE" "$PORT"

echo "▸ 4/4 Esperando al daemon…"
s=""
for _ in $(seq 1 48); do
  s="$("$RUNTIME" exec "$NAME" systemctl is-active hermes-runtime 2>/dev/null || true)"
  { [ "$s" = active ] || [ "$s" = failed ]; } && break
  sleep 5
done
echo "  daemon: ${s:-sin respuesta}"

secret="$("$RUNTIME" exec "$NAME" cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap 2>/dev/null | tr -d '\r\n' || true)"
echo
if [ "$s" = active ] && [ -n "$secret" ]; then
  echo "  ✅ Safent está arriba. Abre:"
  echo "     http://localhost:${PORT}/?k=${secret}"
  echo
  echo "  (el modelo, agentes, MCP y skills se configuran en la UI)"
else
  echo "  ⚠ algo no arrancó. Revisa:  $RUNTIME logs $NAME   |   $RUNTIME exec $NAME journalctl -xe"
  exit 1
fi
