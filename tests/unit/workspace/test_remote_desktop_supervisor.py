"""Tests de RemoteDesktopSupervisor — fallback KasmVNC + degraded detection.

Sin VM, sin Selkies, sin red.
Constitución V.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from hermes.workspace.application.remote_desktop_supervisor import (
    RemoteDesktopSupervisor,
    SupervisorConfig,
)
from hermes.workspace.testing.in_memory_control_plane_channel import (
    InMemoryControlPlaneChannel,
)

pytestmark = pytest.mark.unit


class _FakeGatewayAlive:
    """Fake de gateway que siempre responde ok."""

    supports_audio = True

    async def ping(self, workspace_id: object) -> bool:
        return True

    async def fetch_metrics(self, workspace_id: object) -> dict[str, float]:
        return {"latency_ms": 20.0, "packet_loss_pct": 0.0}

    async def revoke(self, *, workspace_id: object, tenant_id: object) -> None:
        pass


class _FakeGatewayDown:
    """Fake de gateway que no responde (ping falla)."""

    supports_audio = True

    async def ping(self, workspace_id: object) -> bool:
        return False

    async def fetch_metrics(self, workspace_id: object) -> dict[str, float]:
        return {}

    async def revoke(self, *, workspace_id: object, tenant_id: object) -> None:
        pass


class _FakeGatewayHighLoss:
    """Fake de gateway con packet loss > 5% constante."""

    supports_audio = True

    async def ping(self, workspace_id: object) -> bool:
        return True

    async def fetch_metrics(self, workspace_id: object) -> dict[str, float]:
        return {"latency_ms": 80.0, "packet_loss_pct": 15.0}

    async def revoke(self, *, workspace_id: object, tenant_id: object) -> None:
        pass


def _make_supervisor(
    selkies: object,
    kasmvnc: object,
    *,
    heartbeat_interval_s: float = 0.01,
    degraded_window_s: float = 0.02,
) -> tuple[RemoteDesktopSupervisor, InMemoryControlPlaneChannel]:
    channel = InMemoryControlPlaneChannel()
    cfg = SupervisorConfig(
        heartbeat_interval_s=heartbeat_interval_s,
        degraded_window_s=degraded_window_s,
        packet_loss_threshold_pct=5.0,
    )
    supervisor = RemoteDesktopSupervisor(
        workspace_id=uuid4(),
        tenant_id=uuid4(),
        selkies_gateway=selkies,
        kasmvnc_gateway=kasmvnc,
        channel=channel,
        config=cfg,
    )
    return supervisor, channel


class TestFallbackOnGatewayDown:
    """Cuando Selkies cae → fallback a KasmVNC + AuditEntry."""

    async def test_fallback_sets_gateway_to_kasmvnc(self) -> None:
        selkies = _FakeGatewayDown()
        kasmvnc = _FakeGatewayAlive()
        supervisor, channel = _make_supervisor(selkies, kasmvnc)

        await supervisor.start()
        await asyncio.sleep(0.05)
        await supervisor.stop()

        assert supervisor.active_gateway_type() == "kasmvnc"

    async def test_fallback_emits_audit_entry(self) -> None:
        selkies = _FakeGatewayDown()
        kasmvnc = _FakeGatewayAlive()
        supervisor, channel = _make_supervisor(selkies, kasmvnc)

        await supervisor.start()
        await asyncio.sleep(0.05)
        await supervisor.stop()

        assert channel.has_command(
            "audit_entry", audit_kind="remote_desktop_fallback_to_kasmvnc"
        )

    async def test_fallback_happens_only_once(self) -> None:
        selkies = _FakeGatewayDown()
        kasmvnc = _FakeGatewayAlive()
        supervisor, channel = _make_supervisor(selkies, kasmvnc)

        await supervisor.start()
        await asyncio.sleep(0.08)
        await supervisor.stop()

        audit_commands = [
            p
            for _, p in channel.commands
            if p.get("audit_kind") == "remote_desktop_fallback_to_kasmvnc"
        ]
        assert len(audit_commands) == 1


class TestDegradedDetection:
    """packet_loss > 5% sostenido → emite remote_desktop_degraded."""

    async def test_degraded_event_emitted_on_high_loss(self) -> None:
        selkies = _FakeGatewayHighLoss()
        kasmvnc = _FakeGatewayAlive()
        supervisor, channel = _make_supervisor(
            selkies, kasmvnc, heartbeat_interval_s=0.01, degraded_window_s=0.03
        )

        await supervisor.start()
        await asyncio.sleep(0.08)
        await supervisor.stop()

        assert channel.has_command("remote_desktop_degraded")

    async def test_degraded_marked_on_supervisor(self) -> None:
        selkies = _FakeGatewayHighLoss()
        kasmvnc = _FakeGatewayAlive()
        supervisor, channel = _make_supervisor(
            selkies, kasmvnc, heartbeat_interval_s=0.01, degraded_window_s=0.03
        )

        await supervisor.start()
        await asyncio.sleep(0.08)
        await supervisor.stop()

        assert supervisor.is_degraded() or channel.has_command("remote_desktop_degraded")


class TestLatencyExposed:
    """current_latency_ms() retorna la última latencia medida."""

    async def test_latency_populated_after_first_heartbeat(self) -> None:
        selkies = _FakeGatewayAlive()
        kasmvnc = _FakeGatewayAlive()
        supervisor, _ = _make_supervisor(selkies, kasmvnc)

        await supervisor.start()
        await asyncio.sleep(0.03)
        await supervisor.stop()

        assert supervisor.current_latency_ms() == 20.0

    async def test_latency_none_before_first_heartbeat(self) -> None:
        selkies = _FakeGatewayAlive()
        supervisor, _ = _make_supervisor(selkies, _FakeGatewayAlive())
        # Sin iniciar, la latencia es None.
        assert supervisor.current_latency_ms() is None
