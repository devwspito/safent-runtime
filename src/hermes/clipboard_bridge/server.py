"""HTTP server for the clipboard bridge.

Serves on 127.0.0.1:7519 (loopback only).  Two routes:

  POST /clipboard  — write to session clipboard
  GET  /clipboard  — read from session clipboard

Security stance
---------------
* Bound to 127.0.0.1 only: not reachable from the LAN directly.
* Accessed by the noVNC overlay via the same cloudflared / QEMU-hostfwd
  tunnel URL (see exposure requirements in service module docstring).
* CORS header is set to '*'.  This is safe because:
    1. The server never binds to a non-loopback interface.
    2. The only data it handles is the operator's OWN clipboard.
    3. The tunnel URL is secret (cloudflare ephemeral URL) — unknown
       third-party origins cannot guess it.
    4. We have no cookies / sessions / CSRF surface here.
* Clipboard content is never logged (may be an API key or password).
  Only byte lengths appear in log lines.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from hermes.clipboard_bridge.wayland_backend import (
    ClipboardBackend,
    ClipboardError,
    WaylandClipboardBackend,
)

logger = logging.getLogger("hermes-clipboard-bridge")

# 256 KiB — enough for any reasonable clipboard payload (API key, config snippet,
# code block).  Prevents the bridge from being used to DoS wl-copy with huge input.
MAX_CLIPBOARD_BYTES = 256 * 1024

_CORS_HEADERS = {
    # Safe on loopback-only binding — see module docstring.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _send_json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    for k, v in _CORS_HEADERS.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(payload)


def make_handler(backend: ClipboardBackend) -> type[BaseHTTPRequestHandler]:
    """Factory that closes over *backend* so tests can inject a fake."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401
            # Route access logs through structlog-compatible logger, not stderr.
            logger.debug("hermes.clipboard_bridge.http %s", fmt % args)

        def do_OPTIONS(self) -> None:  # noqa: N802
            """Handle CORS preflight from the noVNC overlay."""
            self.send_response(204)
            for k, v in _CORS_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/clipboard":
                _send_json(self, 404, {"error": "not_found"})
                return
            try:
                text = backend.read()
            except ClipboardError as exc:
                logger.warning(
                    "hermes.clipboard_bridge.read_error",
                    extra={"error": str(exc)},
                )
                _send_json(self, 200, {"text": ""})
                return
            # Log length only — content may be a secret.
            logger.info(
                "hermes.clipboard_bridge.get_ok",
                extra={"byte_len": len(text.encode("utf-8"))},
            )
            _send_json(self, 200, {"text": text})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/clipboard":
                _send_json(self, 404, {"error": "not_found"})
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > MAX_CLIPBOARD_BYTES:
                _send_json(self, 413, {"error": "payload_too_large", "max_bytes": MAX_CLIPBOARD_BYTES})
                return

            raw = self.rfile.read(content_length)
            if len(raw) > MAX_CLIPBOARD_BYTES:
                # Defence-in-depth: also check actual bytes read.
                _send_json(self, 413, {"error": "payload_too_large", "max_bytes": MAX_CLIPBOARD_BYTES})
                return

            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                _send_json(self, 400, {"error": "invalid_json"})
                return

            text = body.get("text")
            if not isinstance(text, str):
                _send_json(self, 400, {"error": "field_text_required"})
                return

            encoded = text.encode("utf-8")
            if len(encoded) > MAX_CLIPBOARD_BYTES:
                _send_json(self, 413, {"error": "payload_too_large", "max_bytes": MAX_CLIPBOARD_BYTES})
                return

            try:
                backend.write(text)
            except ClipboardError as exc:
                logger.warning(
                    "hermes.clipboard_bridge.write_error",
                    extra={"error": str(exc)},
                )
                _send_json(self, 503, {"error": "clipboard_unavailable", "detail": str(exc)})
                return

            logger.info(
                "hermes.clipboard_bridge.post_ok",
                extra={"byte_len": len(encoded)},
            )
            _send_json(self, 200, {"ok": True})

    return _Handler


def build_server(
    host: str = "127.0.0.1",
    port: int = 7519,
    backend: ClipboardBackend | None = None,
) -> HTTPServer:
    """Create the HTTPServer with the given backend (defaults to Wayland)."""
    resolved_backend: ClipboardBackend = backend or WaylandClipboardBackend()
    handler_class = make_handler(resolved_backend)
    server = HTTPServer((host, port), handler_class)
    return server
