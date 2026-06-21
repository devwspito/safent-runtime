"""Clipboard server integrado EN el proceso del compositor.

Por qué QClipboard (integración nativa Qt) y no wl-clipboard:
  El compositor es un QtWaylandCompositor. Qt6 SINCRONIZA automáticamente la
  selección Wayland de los clientes con QGuiApplication.clipboard(): cuando una
  app cliente (Chromium, VS Code) copia, su selección aparece en QClipboard del
  compositor, y un setText() en QClipboard se OFRECE a los clientes para pegar.
  wl-copy/wl-paste NO sirven porque requieren wlr-data-control, que Qt no expone.

  (El firewall del SO bloquea :7519 inbound desde la red; el acceso real del
  overlay noVNC entra por el túnel cloudflared = loopback, permitido.)

Thread-safety (Qt):
  QClipboard SOLO se toca en el hilo GUI. El servidor HTTP corre en hilos
  aparte; cada operación se despacha al hilo GUI con
  invokeMethod(QueuedConnection) y se espera por un threading.Event (timeout
  defensivo). Un lock serializa el slot compartido. Los handlers SIEMPRE
  responden (nada de "empty reply").

Contrato HTTP (idéntico — el overlay noVNC no cambia):
  GET  /clipboard            → {"text": "<clipboard del SO>"}
  POST /clipboard {"text"}   → fija el clipboard. 200 {"ok": true}
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QMetaObject, QObject, Qt, Slot
from PySide6.QtGui import QGuiApplication

logger = logging.getLogger("hermes.compositor.clipboard")

_MAX_BODY = 256 * 1024
# Loopback only: el overlay remoto entra por el túnel cloudflared que corre EN el
# guest y conecta a 127.0.0.1:7519. Bindear a 0.0.0.0 expondría el clipboard
# (posible PII) a la LAN. Defensa en profundidad además del firewall del SO.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7519
_GUI_WAIT = 4.0


class _ClipboardProxy(QObject):
    """Vive en el hilo GUI. _run() toca QClipboard de forma segura y deja el
    resultado en self._req (serializado por el lock del caller)."""

    def __init__(self) -> None:
        super().__init__()
        self._req: tuple | None = None  # (op, text, box, event)

    @Slot()
    def _run(self) -> None:
        req = self._req
        if req is None:
            return
        op, text, box, event = req
        try:
            cb = QGuiApplication.clipboard()
            if cb is None:
                pass
            elif op == "get":
                box["v"] = cb.text() or ""
            else:  # set
                cb.setText(text or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("clipboard.qclipboard_failed op=%s: %s", op, exc)
        finally:
            event.set()


_lock = threading.Lock()
_proxy: _ClipboardProxy | None = None


def _dispatch(op: str, text: str = "") -> str:
    if _proxy is None:
        return ""
    box: dict[str, str] = {"v": ""}
    event = threading.Event()
    with _lock:
        _proxy._req = (op, text, box, event)  # noqa: SLF001
        QMetaObject.invokeMethod(_proxy, "_run", Qt.ConnectionType.QueuedConnection)
        event.wait(timeout=_GUI_WAIT)
    return box["v"]


class _Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path.rstrip("/") != "/clipboard":
                self._json(404, {"error": "not found"})
                return
            self._json(200, {"text": _dispatch("get")})
        except Exception as exc:  # noqa: BLE001 — SIEMPRE responder
            logger.warning("clipboard.get_handler_error: %s", exc)
            try:
                self._json(500, {"error": "internal"})
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path.rstrip("/") != "/clipboard":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > _MAX_BODY:
                self._json(413, {"error": "payload too large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                text = str(json.loads(raw or b"{}").get("text", ""))
            except (json.JSONDecodeError, ValueError):
                self._json(400, {"error": "invalid json"})
                return
            _dispatch("set", text)
            self._json(200, {"ok": True})
        except Exception as exc:  # noqa: BLE001
            logger.warning("clipboard.post_handler_error: %s", exc)
            try:
                self._json(500, {"error": "internal"})
            except Exception:  # noqa: BLE001
                pass

    def log_message(self, *_args) -> None:
        return


def start_clipboard_server(
    *, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT
) -> None:
    """Arranca el servidor HTTP del clipboard. Debe llamarse desde el hilo GUI
    del compositor (el _ClipboardProxy queda anclado a ese hilo)."""
    global _proxy
    _proxy = _ClipboardProxy()  # creado en el hilo GUI (caller = main)

    def _serve() -> None:
        try:
            httpd = ThreadingHTTPServer((host, port), _Handler)
            logger.info("hermes.compositor.clipboard_listening %s:%d", host, port)
            httpd.serve_forever()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.compositor.clipboard_server_failed: %s", exc)

    threading.Thread(target=_serve, daemon=True, name="clipboard-server").start()
