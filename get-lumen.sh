#!/bin/sh
# Lumen — instalador de una línea.
#
#   curl -fsSL https://raw.githubusercontent.com/devwspito/lumen-runtime/main/get-lumen.sh | sh
#
# Descarga la imagen endurecida de Lumen, la arranca con la jaula de seguridad
# por defecto (loopback) y abre el navegador en el token único de este arranque.
# El modelo, Composio, Brave, agentes y skills se configuran EN LA UI.
set -eu

IMAGE="${LUMEN_IMAGE:-ghcr.io/devwspito/lumen:latest}"
# Puerto del host: si fijas LUMEN_PORT se respeta; si no, el runtime asigna uno
# LIBRE (dinámico) y lo descubrimos tras arrancar → nunca choca con 17517 ocupado.
PORT="${LUMEN_PORT:-}"
NAME="${LUMEN_NAME:-lumen}"
SECCOMP_URL="${LUMEN_SECCOMP_URL:-https://raw.githubusercontent.com/devwspito/lumen-runtime/main/ops/container/seccomp/lumen.json}"

RT="$(command -v podman 2>/dev/null || command -v docker 2>/dev/null || true)"
[ -n "$RT" ] || { echo "✗ Necesitas podman o docker.  →  https://podman.io/get-started"; exit 1; }
RTN="$(basename "$RT")"
OS="$(uname -s 2>/dev/null || echo unknown)"

# macOS: la jaula (Landlock/netns) corre dentro de una podman machine, que DEBE
# ser rootful. En Linux nativo no hay machine (podman corre directo) → se omite.
if [ "$RTN" = podman ] && [ "$OS" = Darwin ]; then
  # Detectar la machine POR NOMBRE: `podman machine inspect` SIN nombre asume
  # 'podman-machine-default' y falla con "VM does not exist" si la tuya se llama
  # distinto → falso "no hay machine". `list -q` da el nombre real.
  name="$(podman machine list -q 2>/dev/null | head -1)"
  if [ -z "$name" ]; then
    echo "▸ No hay podman machine — creando una rootful (solo la 1ª vez, tarda un poco)…"
    podman machine init --rootful --cpus 4 --memory 8192 --disk-size 60 \
      && podman machine start \
      || { echo "✗ No se pudo crear/arrancar la podman machine."; exit 1; }
    name="$(podman machine list -q 2>/dev/null | head -1)"
  fi
  rootful="$(podman machine inspect "$name" --format '{{.Rootful}}' 2>/dev/null | head -1)"
  if [ "$rootful" != "true" ]; then
    echo "✗ La podman machine '$name' es rootless; la jaula necesita rootful. Conviértela:"
    echo "    podman machine stop '$name' && podman machine set --rootful '$name' && podman machine start '$name'"
    exit 1
  fi
  # Asegurar que está arrancada (si estaba parada, levantarla).
  podman info >/dev/null 2>&1 || podman machine start "$name" >/dev/null 2>&1 || true
fi

# Perfil seccomp (necesario para la jaula). Se descarga bajo $HOME, no /tmp:
# en macOS la podman machine monta $HOME en la VM pero NO /tmp, así que un
# perfil en /tmp no sería visible para `podman run` (corre dentro de la VM).
SECCOMP_DIR="${HOME:-/tmp}/.lumen"
mkdir -p "$SECCOMP_DIR" 2>/dev/null || true
SECCOMP="$SECCOMP_DIR/lumen-seccomp.json"
if ! curl -fsSL "$SECCOMP_URL" -o "$SECCOMP" 2>/dev/null; then
  echo "✗ No se pudo descargar el perfil seccomp ($SECCOMP_URL)."
  echo "  Si el repo aún no es público, exporta LUMEN_SECCOMP_URL a una URL accesible."
  exit 1
fi

echo "▸ Descargando Lumen…"
"$RT" pull "$IMAGE"

echo "▸ Arrancando…"
"$RT" rm -f "$NAME" >/dev/null 2>&1 || true
if [ -n "$PORT" ]; then PUBLISH="127.0.0.1:${PORT}:7517"; else PUBLISH="127.0.0.1::7517"; fi
"$RT" run -d --name "$NAME" --systemd=always \
  -p "$PUBLISH" \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt seccomp="$SECCOMP" \
  --security-opt unmask=/sys/kernel/security \
  --security-opt label=disable \
  -v /sys/kernel/security:/sys/kernel/security:ro \
  -v lumen-data:/var/lib/hermes \
  --shm-size=1g \
  "$IMAGE" >/dev/null

# Descubrir el puerto de host asignado (dinámico si no se fijó LUMEN_PORT).
if [ -z "$PORT" ]; then
  PORT="$("$RT" port "$NAME" 7517 2>/dev/null | head -1 | sed 's/.*:\([0-9][0-9]*\)$/\1/')"
fi
[ -n "$PORT" ] || PORT=17517

echo "▸ Esperando a Lumen…"
secret=""
i=0
while [ "$i" -lt 48 ]; do
  a="$("$RT" exec "$NAME" systemctl is-active hermes-runtime 2>/dev/null || true)"
  if [ "$a" = active ]; then
    secret="$("$RT" exec "$NAME" cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap 2>/dev/null | tr -d '\r\n' || true)"
    [ -n "$secret" ] && break
  fi
  [ "$a" = failed ] && break
  sleep 5
  i=$((i + 1))
done

if [ -z "$secret" ]; then
  echo "  ⚠ Lumen arrancó pero no obtuve el token. Mira:  $RT logs $NAME"
  exit 1
fi

URL="http://localhost:${PORT}/?k=${secret}"
echo ""
echo "  ✅ Lumen está listo:"
echo "     $URL"
echo ""
echo "     (El modelo, Composio, Brave y todo lo demás se configuran en la UI.)"

# Abrir el navegador (el ?k= es único por arranque; nunca se persiste).
if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
fi
