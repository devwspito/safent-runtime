#!/usr/bin/env bash
# Headless GUI self-test for the Safent desktop shell — for boxes with NO display
# (e.g. the DGX / a server). Starts a virtual X display (Xvfb), runs the app with the
# SSE self-test, screenshots the window, and points you at the PNG.
#
# On a real desktop WITH a display (your Mac/Linux desktop), skip this — just run:
#     cd src-tauri && SAFENT_SELFTEST=1 cargo run
#
# Requires: Xvfb, ImageMagick (`import`). On Debian/Ubuntu: apt install xvfb imagemagick
#
# Usage:
#   ./dev-headless-selftest.sh                       # drives the canonical `safent` container
#   SAFENT_URL="http://localhost:PORT/?k=SECRET" ./dev-headless-selftest.sh   # an existing daemon
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/src-tauri/target/debug/safent-desktop"
SHOT="${SHOT:-/tmp/safent-desktop-selftest.png}"
DISP="${DISP:-:99}"
WAIT="${WAIT:-32}"

command -v Xvfb  >/dev/null || { echo "[!] Xvfb no instalado — apt install xvfb"; exit 1; }
command -v import >/dev/null || { echo "[!] ImageMagick 'import' no instalado — apt install imagemagick"; exit 1; }
if [ ! -x "$BIN" ]; then
  echo "[*] Compilando el shell…"; ( cd "$HERE/src-tauri" && cargo build ) || exit 1
fi

pkill -f "Xvfb $DISP" 2>/dev/null; sleep 1
Xvfb "$DISP" -screen 0 1400x950x24 >/tmp/safent-xvfb.log 2>&1 &
XVFB=$!
sleep 2
if ! ps -p "$XVFB" >/dev/null 2>&1; then echo "[!] Xvfb no arrancó — /tmp/safent-xvfb.log:"; tail -5 /tmp/safent-xvfb.log; exit 1; fi

echo "[*] Corriendo el shell bajo $DISP con SSE self-test (softpipe, sin GPU)…"
DISPLAY="$DISP" GDK_BACKEND=x11 \
  WEBKIT_DISABLE_COMPOSITING_MODE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 LIBGL_ALWAYS_SOFTWARE=1 \
  SAFENT_SELFTEST=1 SAFENT_BIN="${SAFENT_BIN:-$(command -v safent || echo "$HERE/../safent")}" \
  "$BIN" >/tmp/safent-desktop.log 2>&1 &
APP=$!

echo "[*] Esperando carga + round-trip SSE (${WAIT}s)…"
sleep "$WAIT"

if ps -p "$APP" >/dev/null 2>&1; then
  echo "[ok] app viva"
else
  echo "[!] la app terminó — /tmp/safent-desktop.log:"; tail -20 /tmp/safent-desktop.log
fi

if DISPLAY="$DISP" import -window root "$SHOT" 2>/dev/null; then
  echo "[ok] Screenshot: $SHOT"
  echo "     Ábrelo: arriba debe salir el banner 'SSE self-test: OK — events=N deltas=M done=true'"
else
  echo "[!] no pude capturar el screenshot"
fi

kill "$APP" "$XVFB" 2>/dev/null
