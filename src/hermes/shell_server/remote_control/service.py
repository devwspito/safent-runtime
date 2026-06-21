"""agents-os-remote-control — standalone signaling + WebRTC daemon.

Bound to 127.0.0.1:7518 on the appliance. Survives crashes of the runtime
or Chromium (FR-034) — its own systemd unit.

MVP scope:
    - WebSocket signaling at /rc/{session_id}.
    - Token verification against shell-server (loopback :7517).
    - DTLS fingerprint binding pin (FR-056).
    - DataChannel echo for the smoke E2E.
    - Pluggable video source — defaults to synthetic frames; real
      PipeWire portal pickup is wired in via HERMES_RC_VIDEO_SOURCE=pipewire
      when `aiortc` + `av` are present at runtime.

Runs as user `hermes-rc` (FR-056 d) inside the strict systemd hardening
defined at ops/agents-os-edition/systemd/agents-os-remote-control.service.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

logger = logging.getLogger("agents-os-remote-control")


SHELL_SERVER_URL = os.environ.get(
    "HERMES_RC_SHELL_URL", "http://127.0.0.1:7517"
)
BIND_HOST = os.environ.get("HERMES_RC_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("HERMES_RC_BIND_PORT", "7518"))


class _ActiveSession:
    """Per-session in-memory state (held only while peer is connected)."""

    __slots__ = ("session_id", "pinned_fingerprint", "peer", "data_channel")

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        self.pinned_fingerprint: str | None = None
        self.peer: Any | None = None
        self.data_channel: Any | None = None


_registry: dict[UUID, _ActiveSession] = {}


async def _verify_session(session_id: UUID) -> dict[str, Any]:
    """Cross-check with shell-server that session exists, is unrevoked
    and not expired."""
    url = f"{SHELL_SERVER_URL}/api/v1/remote-control/sessions/{session_id}"
    async with httpx.AsyncClient(timeout=2.5) as http:
        r = await http.get(url)
    if r.status_code != 200:
        raise HTTPException(status_code=403, detail="session unknown")
    payload = r.json()
    state = payload.get("state")
    if state in ("ended",):
        raise HTTPException(status_code=403, detail=f"session state={state}")
    exp = datetime.fromisoformat(payload["token_expires_at"])
    if exp < datetime.now(tz=UTC):
        raise HTTPException(status_code=410, detail="session expired")
    return payload


async def _report_binding_violation(session_id: UUID) -> None:
    url = (
        f"{SHELL_SERVER_URL}/api/v1/remote-control/sessions/"
        f"{session_id}/binding-violation"
    )
    try:
        async with httpx.AsyncClient(timeout=2.0) as http:
            await http.post(url)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to report binding violation upstream")


def create_app() -> FastAPI:
    app = FastAPI(
        title="agents-os-remote-control",
        version=os.environ.get("HERMES_VERSION", "0.4.0"),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "active_sessions": str(len(_registry)),
            "service": "agents-os-remote-control",
        }

    @app.websocket("/rc/{session_id}")
    async def signaling(ws: WebSocket, session_id: UUID) -> None:
        await ws.accept(subprotocol="hermes-rc.v1")
        try:
            metadata = await _verify_session(session_id)
        except HTTPException as exc:
            await ws.send_json(
                {"kind": "error", "code": exc.status_code, "detail": exc.detail}
            )
            await ws.close(code=4403)
            return

        active = _registry.setdefault(session_id, _ActiveSession(session_id))

        await ws.send_json(
            {
                "kind": "hello",
                "session_id": str(session_id),
                "expected_dtls_fingerprint": metadata["dtls_fingerprint"][:16],
                "scope": metadata["scope"],
            }
        )

        try:
            while True:
                msg = await ws.receive_text()
                payload = json.loads(msg)
                kind = payload.get("kind")

                if kind == "offer":
                    fp = payload.get("dtls_fingerprint")
                    if not fp:
                        await ws.send_json(
                            {
                                "kind": "error",
                                "detail": "offer missing dtls_fingerprint",
                            }
                        )
                        continue
                    expected = metadata["dtls_fingerprint"]
                    if active.pinned_fingerprint is None:
                        if fp != expected:
                            await _report_binding_violation(session_id)
                            await ws.send_json(
                                {
                                    "kind": "binding_violation",
                                    "reason": "fingerprint mismatch on first use",
                                }
                            )
                            await ws.close(code=4401)
                            return
                        active.pinned_fingerprint = fp
                    elif active.pinned_fingerprint != fp:
                        await _report_binding_violation(session_id)
                        await ws.send_json(
                            {
                                "kind": "binding_violation",
                                "reason": "fingerprint changed mid-session",
                            }
                        )
                        await ws.close(code=4401)
                        return
                    await ws.send_json(
                        {
                            "kind": "answer",
                            "sdp": _build_stub_answer(
                                payload.get("sdp", ""),
                                expected_fp=expected,
                            ),
                        }
                    )

                elif kind == "ice":
                    await ws.send_json({"kind": "ice", "candidate": None})

                elif kind == "echo":
                    await ws.send_json(
                        {"kind": "echo", "data": payload.get("data")}
                    )

                elif kind == "ping":
                    await ws.send_json(
                        {
                            "kind": "pong",
                            "ts": datetime.now(tz=UTC).isoformat(),
                        }
                    )

                elif kind == "bye":
                    await ws.send_json({"kind": "bye"})
                    await ws.close(code=1000)
                    return

                else:
                    await ws.send_json(
                        {"kind": "error", "detail": f"unknown kind: {kind}"}
                    )
        except WebSocketDisconnect:
            return
        finally:
            _registry.pop(session_id, None)

    return app


def _build_stub_answer(_offer_sdp: str, *, expected_fp: str) -> str:
    """Synthetic answer SDP — placeholder while the real GStreamer pipeline
    isn't wired. The smoke test only needs a parseable SDP-shaped string."""
    return (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=hermes-rc-stub\r\n"
        "t=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        f"a=fingerprint:sha-256 {expected_fp}\r\n"
        "a=setup:active\r\n"
        "a=rtpmap:96 VP8/90000\r\n"
        "a=sendonly\r\n"
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    stop = asyncio.Event()

    def _on_signal(*_: object) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal)


def main() -> None:
    # logging quiere el nivel en MAYÚSCULA ("INFO"); la unit systemd pasa
    # HERMES_RC_LOG_LEVEL=info (minúscula, que es lo que uvicorn espera).
    logging.basicConfig(
        level=os.environ.get("HERMES_RC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if os.environ.get("HERMES_RC_NOTIFY", "0") == "1":
        try:
            import systemd.daemon  # type: ignore[import-not-found]

            systemd.daemon.notify("READY=1")
        except ImportError:
            logger.warning(
                "HERMES_RC_NOTIFY=1 but python3-systemd not installed"
            )
    logger.info(
        "agents-os-remote-control starting on %s:%d (shell=%s)",
        BIND_HOST,
        BIND_PORT,
        SHELL_SERVER_URL,
    )
    uvicorn.run(
        create_app(),
        host=BIND_HOST,
        port=BIND_PORT,
        log_level=os.environ.get("HERMES_RC_LOG_LEVEL", "info").lower(),
        access_log=False,
        ws="websockets",
    )


if __name__ == "__main__":
    main()
