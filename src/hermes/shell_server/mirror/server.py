"""MirrorServer — sirve el espejo (frames JPEG) + recibe input, por WebSocket.

Auth layers (both must pass for WebSocket upgrade)
----------------------------------------------------
Layer 1 — per-session token (?token=…):
  A cryptographically-strong token (≥128-bit, base-58, generated at first boot
  by hermes-remote-token-gen.service and written to /etc/hermes/remote-token).
  Verified with hmac.compare_digest (constant-time, CWE-208).

Layer 2 — Cloudflare Access JWT (Cf-Access-Jwt-Assertion header):
  When reached via the named Cloudflare tunnel, Cloudflare Access injects a
  signed JWT into every request that passes the Access policy check.  The
  OS-side verifier checks the RS256 signature against the team JWKS, and
  validates audience and expiry.

  If CF_ACCESS_AUD / CF_ACCESS_TEAM_DOMAIN are NOT configured, layer 2 is
  ABSENT — the server logs a warning and DENIES all WebSocket connections
  (fail-closed).  The operator must set up Cloudflare Access before enabling
  the remote tunnel.

  To bypass layer 2 for local-only use (no tunnel), set:
      HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL=true
  This disables JWT verification ONLY when the connection arrives on the
  loopback interface (127.0.0.1).  Do NOT set this in production with a
  public tunnel.

HTTP routes:
  GET  /            -> viewer.html (exige ?token=…)
  WS   /ws?token=…  -> binario: frames JPEG hacia el cliente
                       texto JSON: eventos de input desde el cliente
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve

from .cf_access_verifier import (
    CfAccessError,
    CfAccessNotConfigured,
    CloudflareAccessVerifier,
)
from .frame_source_port import FrameSourcePort
from .input_effector_port import SeatInputEffectorPort
from .jpeg_source import JpegFrameSource
from .mutter_mirror import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, MutterMirrorSession

logger = logging.getLogger("hermes-mirror")

_STATIC = Path(__file__).parent / "static"
_BUTTONS = {0: BTN_LEFT, 1: BTN_MIDDLE, 2: BTN_RIGHT}
_FRAME_INTERVAL = 0.07  # ~14 fps

# Set HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL=true only for pure-LAN use
# without the Cloudflare tunnel.  Never set on internet-facing deployments.
_BYPASS_LOCAL = os.environ.get("HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL", "").lower() in (
    "true", "1", "yes"
)


class MirrorServer:
    def __init__(
        self,
        *,
        source: FrameSourcePort | JpegFrameSource,
        mirror: SeatInputEffectorPort | MutterMirrorSession,
        token: str,
        host: str = "127.0.0.1",
        port: int = 6080,
        cf_verifier: CloudflareAccessVerifier | None = None,
    ) -> None:
        self._source = source
        self._mirror = mirror
        self._token = token
        self._host = host
        self._port = port
        self._viewer = (_STATIC / "viewer.html").read_bytes()
        self._cf = cf_verifier or CloudflareAccessVerifier()

    async def _process_request(self, connection, request):
        path = request.path.split("?", 1)[0]
        if path.startswith("/ws"):
            return None  # deja que upgrade a WebSocket
        if path in ("/", "/index.html", "/viewer.html"):
            # respond() pone text/plain por defecto → el navegador mostraría el
            # HTML como texto. Forzamos text/html.
            resp = connection.respond(HTTPStatus.OK, self._viewer.decode("utf-8"))
            del resp.headers["Content-Type"]
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

    def _authed_token(self, ws) -> bool:
        """Layer 1: per-session token check (constant-time compare, CWE-208)."""
        import hmac  # noqa: PLC0415 — stdlib, safe to import inside
        q = (ws.request.path.split("?", 1) + [""])[1]
        params = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
        candidate = params.get("token", "")
        return hmac.compare_digest(candidate, self._token)

    def _cf_access_header(self, ws) -> str:
        """Extract Cf-Access-Jwt-Assertion from request headers (case-insensitive)."""
        headers = ws.request.headers
        # websockets headers object is case-insensitive; try the canonical form.
        return headers.get("Cf-Access-Jwt-Assertion", "")

    def _is_loopback(self, ws) -> bool:
        """Return True when the remote peer is the loopback address."""
        try:
            return ws.remote_address[0] in ("127.0.0.1", "::1")
        except Exception:  # noqa: BLE001 — defensive
            return False

    def _authed_cf_access(self, ws) -> bool:
        """Layer 2: Cloudflare Access JWT verification.

        Returns True if:
          - BYPASS_LOCAL is set AND the peer is loopback (local-only mode), OR
          - The JWT is present, signature valid, aud correct, not expired.

        Returns False (deny) in ALL other cases including config absent.
        Never raises — all errors are caught and translated to False + log.
        """
        if _BYPASS_LOCAL and self._is_loopback(ws):
            logger.debug("hermes.mirror.cf_access.bypass_local")
            return True

        raw_jwt = self._cf_access_header(ws)
        try:
            claims = self._cf.verify(raw_jwt)
            logger.info(
                "hermes.mirror.cf_access.ok",
                extra={"email": claims.email, "sub": claims.sub},
            )
            return True
        except CfAccessNotConfigured as exc:
            logger.error(
                "hermes.mirror.cf_access.not_configured — "
                "set CF_ACCESS_TEAM_DOMAIN + CF_ACCESS_AUD in "
                "/etc/hermes/cf-access.env: %s",
                exc,
            )
            return False
        except CfAccessError as exc:
            logger.warning("hermes.mirror.cf_access.denied: %s", exc)
            return False

    async def _handler(self, ws):
        # Layer 1 MUST pass first (constant-time, no I/O needed).
        if not self._authed_token(ws):
            logger.warning("hermes.mirror.auth.bad_token remote=%s", ws.remote_address)
            await ws.close(code=4401, reason="bad token")
            return

        # Layer 2: Cloudflare Access JWT (may block briefly for JWKS fetch on
        # cache miss — happens at most every 5 minutes across all connections).
        loop = asyncio.get_event_loop()
        cf_ok = await loop.run_in_executor(None, self._authed_cf_access, ws)
        if not cf_ok:
            logger.warning("hermes.mirror.auth.cf_access_denied remote=%s", ws.remote_address)
            await ws.close(code=4403, reason="access denied")
            return

        logger.info("hermes.mirror.session.start remote=%s", ws.remote_address)
        sender = asyncio.create_task(self._send_frames(ws))
        try:
            async for msg in ws:
                await self._handle_input(msg)
        finally:
            sender.cancel()
            logger.info("hermes.mirror.session.end remote=%s", ws.remote_address)

    async def _send_frames(self, ws) -> None:
        # Reenvía el último frame a ritmo constante (aunque no cambie): mutter
        # solo emite frame en damage, así el cliente siempre tiene la pantalla
        # actual y el input se siente responsivo.
        while True:
            data, _size = self._source.latest()
            if data is not None:
                await ws.send(data)
            await asyncio.sleep(_FRAME_INTERVAL)

    async def _handle_input(self, msg) -> None:
        if isinstance(msg, bytes):
            return
        try:
            ev = json.loads(msg)
        except (ValueError, TypeError):
            return
        loop = asyncio.get_event_loop()
        t = ev.get("t")
        m = self._mirror
        try:
            if t == "m":
                await loop.run_in_executor(
                    None, m.pointer_motion, float(ev["x"]), float(ev["y"])
                )
            elif t == "b":
                btn = _BUTTONS.get(int(ev["b"]))
                if btn is not None:
                    await loop.run_in_executor(
                        None, m.pointer_button, btn, bool(ev["p"])
                    )
            elif t == "kc":
                # Teclado por KEYCODE evdev (preferido: sin latch de shift).
                await loop.run_in_executor(
                    None, m.keyboard_keycode, int(ev["c"]), bool(ev["p"])
                )
            elif t == "paste":
                # Pegar contenido del Mac en la VM: ponemos su texto en el
                # portapapeles de la VM (wl-copy) y disparamos Ctrl+V.
                text = str(ev.get("text", ""))
                if text:
                    await loop.run_in_executor(None, self._paste_into_vm, text)
            elif t == "k":
                # Compat: teclado por keysym (legacy).
                await loop.run_in_executor(
                    None, m.keyboard_keysym, int(ev["k"]), bool(ev["p"])
                )
            elif t == "w":
                await loop.run_in_executor(
                    None, m.pointer_axis_discrete, int(ev["a"]), int(ev["s"])
                )
        except Exception:  # noqa: BLE001 - un input malo no mata la sesión
            logger.debug("input inject falló", exc_info=True)

    def _paste_into_vm(self, text: str) -> None:
        """Pone `text` (clipboard del cliente) en el portapapeles de la VM y
        pega con Ctrl+V. Corre en la sesión Wayland (tiene WAYLAND_DISPLAY)."""
        try:
            subprocess.run(
                ["wl-copy", "--type", "text/plain"],
                input=text.encode("utf-8"),
                timeout=4,
                check=False,
            )
        except Exception:  # noqa: BLE001 - sin wl-clipboard seguimos con el tecleo
            logger.debug("wl-copy falló", exc_info=True)
        # Ctrl+V (keycodes evdev: LeftCtrl=29, V=47).
        m = self._mirror
        for code, pressed in ((29, True), (47, True), (47, False), (29, False)):
            try:
                m.keyboard_keycode(code, pressed)
            except Exception:  # noqa: BLE001
                logger.debug("inyectar Ctrl+V falló", exc_info=True)

    async def run(self) -> None:
        logger.info("mirror server en %s:%s", self._host, self._port)
        async with serve(
            self._handler,
            self._host,
            self._port,
            process_request=self._process_request,
            max_size=2**20,
        ):
            await asyncio.Future()  # corre para siempre
