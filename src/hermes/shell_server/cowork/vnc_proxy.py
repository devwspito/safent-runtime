"""vnc_proxy — bridge the web UI's noVNC WebSocket to the jailed browser's x11vnc.

The jailed Chromium runs HEADFUL on an Xvfb display; x11vnc serves that display as
raw RFB on the netns veth IP (10.200.0.2:5900), reachable ONLY by the daemon (nft
rule, same trust model as the CDP port). Browsers can't open a raw TCP socket, so
noVNC speaks RFB over a WebSocket — this endpoint is the websockify bridge: it
authenticates with the webui bearer, opens the TCP RFB connection, and pipes bytes
both ways. This is the industry-standard live-view (Kasm/neko): sharp + fluid real
display pixels, native RFB input — not the blurry/slow CDP screencast.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("hermes.shell_server.cowork.vnc_proxy")

_VNC_HOST = os.environ.get("BROWSER_VNC_HOST", "10.200.0.2")
_VNC_PORT = int(os.environ.get("BROWSER_VNC_PORT", "5900"))
_CHUNK = 65536


def create_vnc_proxy_router() -> APIRouter:
    from hermes.shell_server.cowork.training_live import (  # noqa: PLC0415
        _try_ensure_browser_running,
        _verify_token,
    )

    router = APIRouter()

    @router.websocket("/api/v1/vnc")
    async def vnc(websocket: WebSocket) -> None:
        webui_token: str = getattr(websocket.app.state, "shell_webui_token", "")
        if not _verify_token(websocket.query_params.get("token", ""), webui_token):
            await websocket.close(code=1008, reason="unauthorized")
            return
        # noVNC negotiates the 'binary' subprotocol; echo it back if offered.
        subs = websocket.scope.get("subprotocols") or []
        sub = "binary" if "binary" in subs else None
        await websocket.accept(subprotocol=sub)

        await _try_ensure_browser_running()  # bring the jailed headful browser up

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(_VNC_HOST, _VNC_PORT), timeout=20
            )
        except Exception:  # noqa: BLE001
            logger.warning("hermes.vnc_proxy.rfb_unreachable %s:%s", _VNC_HOST, _VNC_PORT)
            try:
                await websocket.close(code=1011, reason="vnc unreachable")
            except Exception:  # noqa: BLE001
                pass
            return

        logger.info("hermes.vnc_proxy.session.start remote=%s", websocket.client)

        async def ws_to_tcp() -> None:
            try:
                while True:
                    data = await websocket.receive_bytes()
                    writer.write(data)
                    await writer.drain()
            except (WebSocketDisconnect, RuntimeError, ConnectionError):
                return
            except Exception:  # noqa: BLE001
                return

        async def tcp_to_ws() -> None:
            try:
                while True:
                    data = await reader.read(_CHUNK)
                    if not data:
                        return
                    await websocket.send_bytes(data)
            except Exception:  # noqa: BLE001
                return

        t1 = asyncio.create_task(ws_to_tcp())
        t2 = asyncio.create_task(tcp_to_ws())
        try:
            done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            logger.info("hermes.vnc_proxy.session.end remote=%s", websocket.client)

    return router
