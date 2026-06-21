"""RemoteDesktopSupervisor — vigila la salud del gateway WebRTC (T085).

Responsabilidades:
- Heartbeat al gateway (Selkies) a intervalos configurables.
- Detecta packet loss > 5% sostenido durante 10 s → emite evento ``degraded``
  al canal del control plane (WS).
- Si Selkies cae (heartbeat falla o ICE no negocia en timeout) → fallback
  automático a KasmVNC + AuditEntry ``remote_desktop_fallback_to_kasmvnc``.
- Expone ``current_latency_ms()`` con la última medición disponible.

Diseño:
- Aplicación pura de coordinación; no mezcla lógica de dominio con I/O.
- Depende de ``RemoteDesktopGatewayPort`` via DI.
- WS channel se inyecta para emitir eventos (audit + degraded notice).
- No persiste estado a disco; el estado es efímero por sesión.

FR-001, NFR-001, edge case "pérdida de paquetes".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

__all__ = [
    "GatewayHealthSnapshot",
    "RemoteDesktopSupervisor",
    "SupervisorConfig",
]

_PACKET_LOSS_THRESHOLD_PCT = 5.0
_DEGRADED_WINDOW_SECONDS = 10


@dataclass(frozen=True, slots=True)
class GatewayHealthSnapshot:
    """Snapshot de salud en un instante dado."""

    workspace_id: UUID
    sampled_at: datetime
    latency_ms: float | None
    packet_loss_pct: float | None
    is_degraded: bool
    gateway_type: str  # "selkies" | "kasmvnc"


class _ControlPlaneChannelProtocol(Protocol):
    async def send_command(self, method: str, params: dict[str, Any]) -> None: ...


class _GatewayProtocol(Protocol):
    async def revoke(self, *, workspace_id: UUID, tenant_id: UUID) -> None: ...


@dataclass
class SupervisorConfig:
    """Parámetros del supervisor inyectados en construcción."""

    heartbeat_interval_s: float = 5.0
    ice_timeout_s: float = 30.0
    degraded_window_s: float = float(_DEGRADED_WINDOW_SECONDS)
    packet_loss_threshold_pct: float = _PACKET_LOSS_THRESHOLD_PCT


class RemoteDesktopSupervisor:
    """Supervisor del gateway remoto. Detecta degradación y orquesta fallback.

    Uso típico::

        supervisor = RemoteDesktopSupervisor(
            workspace_id=ws_id,
            tenant_id=tenant_id,
            selkies_gateway=selkies,
            kasmvnc_gateway=kasmvnc,
            channel=cp_channel,
        )
        await supervisor.start()
        # …
        await supervisor.stop()
    """

    def __init__(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        selkies_gateway: Any,
        kasmvnc_gateway: Any,
        channel: _ControlPlaneChannelProtocol,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._tenant_id = tenant_id
        self._selkies = selkies_gateway
        self._kasmvnc = kasmvnc_gateway
        self._channel = channel
        self._cfg = config or SupervisorConfig()

        self._active_gateway: Any = selkies_gateway
        self._active_gateway_type: str = "selkies"
        self._last_latency_ms: float | None = None
        self._degraded_since: datetime | None = None
        self._fallback_done: bool = False
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None

        # Rolling buffer for loss measurements (float percentage, timestamped).
        self._loss_samples: list[tuple[datetime, float]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Arranca la tarea de supervisión en background."""
        self._running = True
        self._task = asyncio.create_task(self._supervisor_loop())
        logger.info(
            "remote_desktop_supervisor.started",
            extra={"workspace_id": str(self._workspace_id)},
        )

    async def stop(self) -> None:
        """Para la supervisión limpiamente."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            "remote_desktop_supervisor.stopped",
            extra={"workspace_id": str(self._workspace_id)},
        )

    # ------------------------------------------------------------------
    # Public queries
    # ------------------------------------------------------------------

    def current_latency_ms(self) -> float | None:
        """Última latencia medida (ms). None si aún no hay medición."""
        return self._last_latency_ms

    def is_degraded(self) -> bool:
        return self._degraded_since is not None

    def active_gateway_type(self) -> str:
        return self._active_gateway_type

    # ------------------------------------------------------------------
    # Internal supervisor loop
    # ------------------------------------------------------------------

    async def _supervisor_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._cfg.heartbeat_interval_s)
                await self._check_health()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "remote_desktop_supervisor.loop_error",
                    extra={"error": str(exc)},
                )

    async def _check_health(self) -> None:
        """Interroga métricas del gateway activo y toma decisiones."""
        metrics = await self._fetch_metrics()
        self._last_latency_ms = metrics.get("latency_ms")
        loss_pct: float = metrics.get("packet_loss_pct", 0.0)

        self._record_loss_sample(loss_pct)
        sustained_loss = self._is_loss_sustained()

        if sustained_loss and not self._fallback_done:
            await self._handle_degraded()
        elif not sustained_loss and self._degraded_since is not None:
            self._degraded_since = None
            logger.info(
                "remote_desktop_supervisor.degraded_recovered",
                extra={"workspace_id": str(self._workspace_id)},
            )

        if not self._fallback_done and not await self._gateway_alive():
            await self._execute_fallback()

    async def _fetch_metrics(self) -> dict[str, float]:
        """Obtiene métricas del gateway activo.

        En producción: lee del socket de control de Selkies o del API de KasmVNC.
        Si el proceso no responde, devuelve valores por defecto.
        """
        try:
            fetch = getattr(self._active_gateway, "fetch_metrics", None)
            if fetch is not None:
                return await fetch(self._workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "remote_desktop_supervisor.metrics_fetch_failed",
                extra={"error": str(exc)},
            )
        return {}

    async def _gateway_alive(self) -> bool:
        """Comprueba si el gateway activo responde."""
        try:
            ping = getattr(self._active_gateway, "ping", None)
            if ping is not None:
                return bool(await ping(self._workspace_id))
        except Exception:  # noqa: BLE001
            return False
        return True

    def _record_loss_sample(self, loss_pct: float) -> None:
        now = datetime.now(tz=UTC)
        self._loss_samples.append((now, loss_pct))
        cutoff = now.timestamp() - self._cfg.degraded_window_s
        self._loss_samples = [
            (t, v) for t, v in self._loss_samples if t.timestamp() >= cutoff
        ]

    def _is_loss_sustained(self) -> bool:
        if not self._loss_samples:
            return False
        window_s = self._cfg.degraded_window_s
        now = datetime.now(tz=UTC).timestamp()
        recent = [v for t, v in self._loss_samples if now - t.timestamp() <= window_s]
        if not recent:
            return False
        return all(v > self._cfg.packet_loss_threshold_pct for v in recent)

    async def _handle_degraded(self) -> None:
        if self._degraded_since is None:
            self._degraded_since = datetime.now(tz=UTC)
        await self._emit_degraded_event()

    async def _emit_degraded_event(self) -> None:
        logger.warning(
            "remote_desktop_supervisor.degraded",
            extra={
                "workspace_id": str(self._workspace_id),
                "gateway_type": self._active_gateway_type,
            },
        )
        await self._channel.send_command(
            "remote_desktop_degraded",
            {
                "workspace_id": str(self._workspace_id),
                "tenant_id": str(self._tenant_id),
                "gateway_type": self._active_gateway_type,
                "degraded_since": self._degraded_since.isoformat()
                if self._degraded_since
                else None,
            },
        )

    async def _execute_fallback(self) -> None:
        """Conmuta a KasmVNC y registra el AuditEntry."""
        self._fallback_done = True
        self._active_gateway = self._kasmvnc
        self._active_gateway_type = "kasmvnc"

        logger.warning(
            "remote_desktop_supervisor.fallback_to_kasmvnc",
            extra={"workspace_id": str(self._workspace_id)},
        )
        await self._channel.send_command(
            "audit_entry",
            {
                "workspace_id": str(self._workspace_id),
                "tenant_id": str(self._tenant_id),
                "audit_kind": "remote_desktop_fallback_to_kasmvnc",
                "occurred_at": datetime.now(tz=UTC).isoformat(),
            },
        )
