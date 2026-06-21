"""WebRTC Signaling Client — TLS 1.3 + cert pinning + DTLS-SRTP (T086).

BLOQUEANTE C-3 / FR-061b.

Responsabilidades:
- Cliente de señalización WebRTC con el control plane.
- TLS 1.3 obligatorio + cert pinning de la CA del control plane.
- DTLS-SRTP: el fingerprint del canal se enlaza al ``subscription_token``
  efímero del workspace (binding DTLS fingerprint ↔ token).
- TURN credentials efímeras scoped al ``workspace_id`` (mintadas en boot).
- Fingerprint mismatch → close del canal + AuditEntry ``webrtc_fingerprint_mismatch``.

Lazy-imports de cualquier binding WebRTC (aiortc, aioice…) — la carga no falla
si las deps no están instaladas.

Constitución IV: fail-closed en todo vector de autenticación.
FR-038: tenant_id propagado en todos los mensajes de señalización.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

__all__ = [
    "WebRtcSignalingClient",
    "WebRtcSignalingConfig",
    "FingerprintMismatch",
    "TurnCredentials",
]


class WebRtcSignalingError(RuntimeError):
    """Base."""


class FingerprintMismatch(WebRtcSignalingError):
    """DTLS fingerprint no coincide con el binding del token efímero.

    Acción: cerrar canal + AuditEntry ``webrtc_fingerprint_mismatch``.
    """


class TurnCredentialsExpired(WebRtcSignalingError):
    """TURN credentials han caducado; solicitar nuevas al control plane."""


@dataclass(frozen=True, slots=True)
class TurnCredentials:
    """Credenciales TURN efímeras scoped al workspace."""

    workspace_id: UUID
    tenant_id: UUID
    username: str
    credential: str
    turn_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WebRtcSignalingConfig:
    """Configuración del cliente inyectada en boot."""

    signaling_url: str             # wss://signaling.hermes.internal/ws
    workspace_id: UUID
    tenant_id: UUID
    subscription_token: str        # token efímero del workspace; fingerprint se vincula aquí
    ca_fingerprint_sha256_hex: str # 64 hex — CA del control plane
    client_cert_pem: bytes
    client_key_pem: bytes
    turn_credentials: TurnCredentials
    tls_min_version: str = "TLSv1.3"


class WebRtcSignalingClient:
    """Cliente de señalización WebRTC con TLS 1.3 + cert pinning + DTLS binding.

    Flujo:
    1. Conecta via ``wss://`` con TLS 1.3 + cert mTLS.
    2. Verifica fingerprint del peer contra ``ca_fingerprint_sha256_hex``.
    3. Durante el setup de la sesión WebRTC, calcula el fingerprint DTLS
       del peer y lo compara contra el hash del ``subscription_token``.
    4. Si mismatch → ``close()`` + emite ``audit_entry webrtc_fingerprint_mismatch``.
    5. TURN credentials se incluyen en el SDP/ICE offer.
    """

    def __init__(
        self,
        config: WebRtcSignalingConfig,
        channel: Any,  # ControlPlaneChannelPort; inyectado
    ) -> None:
        self._cfg = config
        self._channel = channel
        self._ws: Any = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Abre conexión de señalización. Lanza si TLS o fingerprint fallan."""
        self._ws = await self._open_tls_ws()
        logger.info(
            "webrtc_signaling.connected",
            extra={
                "workspace_id": str(self._cfg.workspace_id),
                "url": self._cfg.signaling_url,
            },
        )

    async def close(self) -> None:
        """Cierra canal limpiamente."""
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "webrtc_signaling.close_error", extra={"error": str(exc)}
                )
        logger.info(
            "webrtc_signaling.closed",
            extra={"workspace_id": str(self._cfg.workspace_id)},
        )

    # ------------------------------------------------------------------
    # Signaling messages
    # ------------------------------------------------------------------

    async def send_offer(self, sdp: str) -> None:
        """Envía SDP offer con TURN credentials embebidas."""
        await self._send(
            {
                "type": "offer",
                "sdp": sdp,
                "turn_url": self._cfg.turn_credentials.turn_url,
                "turn_username": self._cfg.turn_credentials.username,
                # credential nunca en logs (structlog filtra 'credential')
                "turn_credential": self._cfg.turn_credentials.credential,
                "workspace_id": str(self._cfg.workspace_id),
                "tenant_id": str(self._cfg.tenant_id),
            }
        )

    async def receive_answer(self) -> dict[str, Any]:
        """Espera SDP answer del signaling server."""
        if self._ws is None:
            raise WebRtcSignalingError("Canal no conectado")
        raw = await self._ws.recv()
        return json.loads(raw)  # type: ignore[no-any-return]

    async def verify_dtls_fingerprint(self, peer_dtls_fingerprint_hex: str) -> None:
        """Verifica el fingerprint DTLS del peer contra el binding del token.

        El binding se calcula como SHA-256(subscription_token + workspace_id).
        Si no coincide → close + AuditEntry.
        """
        expected = self._compute_expected_fingerprint()
        if not hmac.compare_digest(
            peer_dtls_fingerprint_hex.lower(), expected.lower()
        ):
            await self._emit_fingerprint_mismatch_audit(peer_dtls_fingerprint_hex)
            await self.close()
            raise FingerprintMismatch(
                f"DTLS fingerprint mismatch: esperado ...{expected[-8:]}, "
                f"recibido ...{peer_dtls_fingerprint_hex[-8:]}"
            )
        logger.info(
            "webrtc_signaling.dtls_fingerprint_ok",
            extra={"workspace_id": str(self._cfg.workspace_id)},
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_expected_fingerprint(self) -> str:
        """SHA-256(subscription_token || workspace_id_bytes)."""
        material = (
            self._cfg.subscription_token + str(self._cfg.workspace_id)
        ).encode()
        return hashlib.sha256(material).hexdigest()

    async def _emit_fingerprint_mismatch_audit(
        self, received_fp: str
    ) -> None:
        logger.error(
            "webrtc_signaling.fingerprint_mismatch",
            extra={
                "workspace_id": str(self._cfg.workspace_id),
                "tenant_id": str(self._cfg.tenant_id),
                "received_fp_suffix": received_fp[-8:] if received_fp else "none",
            },
        )
        if self._channel is not None:
            await self._channel.send_command(
                "audit_entry",
                {
                    "workspace_id": str(self._cfg.workspace_id),
                    "tenant_id": str(self._cfg.tenant_id),
                    "audit_kind": "webrtc_fingerprint_mismatch",
                    "occurred_at": datetime.now(tz=UTC).isoformat(),
                },
            )

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._closed:
            raise WebRtcSignalingError("Canal no conectado o cerrado")
        await self._ws.send(json.dumps(payload))

    async def _open_tls_ws(self) -> Any:
        """Abre WebSocket con TLS 1.3 + cert pinning.

        Lazy-import de ``websockets`` y ``ssl``.
        """
        import os  # noqa: PLC0415
        import ssl  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import websockets  # noqa: PLC0415

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED

        # Fix-11: use mkstemp (atomic O_EXCL creation, 0600 by default) instead of
        # mktemp (TOCTOU race — the path could be claimed between mktemp and open).
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(cert_fd, "wb") as f:
                f.write(self._cfg.client_cert_pem)
            with os.fdopen(key_fd, "wb") as f:
                f.write(self._cfg.client_key_pem)
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        finally:
            for p in (cert_path, key_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

        ws = await websockets.connect(
            self._cfg.signaling_url,
            ssl=ctx,
            max_size=2**18,
        )
        # Cert pinning: verificar SHA-256 del cert del peer.
        peer_cert_der = (
            ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        )
        peer_fp = hashlib.sha256(peer_cert_der).hexdigest()
        if not hmac.compare_digest(
            peer_fp.lower(), self._cfg.ca_fingerprint_sha256_hex.lower()
        ):
            await ws.close()
            raise FingerprintMismatch(
                "TLS CA pinning falló: fingerprint del peer no coincide"
            )
        return ws


def mint_turn_credentials(
    *,
    workspace_id: UUID,
    tenant_id: UUID,
    signing_key: bytes,
    turn_url: str,
    ttl_seconds: int = 3600,
) -> TurnCredentials:
    """Minta credenciales TURN efímeras scoped al workspace.

    username = "workspace_id:exp_unix_ts"
    credential = HMAC-SHA256(username, signing_key) hex
    """
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
    exp_ts = int(expires_at.timestamp())
    username = f"{workspace_id}:{exp_ts}"
    credential = hmac.new(
        signing_key, username.encode(), hashlib.sha256
    ).hexdigest()
    return TurnCredentials(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        username=username,
        credential=credential,
        turn_url=turn_url,
        expires_at=expires_at,
    )
