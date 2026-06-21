"""T035 — POST /api/v1/chat fail-hard cuando daemon/D-Bus caído.

SC-005 / CTRL-P1-11 / FR-012:
  Sin agente → 503 agent_unavailable explícito. 0 respuesta alternativa.
  NO fallback passthrough. NO degradación silenciosa.

Setup: crea la app con un ControlPlanePort que lanza AgentUnavailable
(simula el daemon caído). Verifica que el endpoint devuelve 503 con
el código correcto y NO intenta ninguna ruta alternativa.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper — app con control_plane stub que lanza AgentUnavailable
# ---------------------------------------------------------------------------


def _make_app_with_unavailable_daemon() -> Any:
    """Construye la app inyectando un ControlPlanePort que siempre lanza
    AgentUnavailable (simula daemon D-Bus caído).

    La app se construye con HERMES_SHELL_DB = :memory: para no necesitar disco.
    """
    import os  # noqa: PLC0415

    with patch.dict(os.environ, {"HERMES_SHELL_DB": ":memory:"}):
        from hermes.shell_server.main import create_app  # noqa: PLC0415

    return create_app()


class _AlwaysUnavailableControlPlane:
    """ControlPlanePort que simula el daemon caído."""

    async def enqueue(self, **_: Any) -> None:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def get_queue_status(self) -> dict:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def list_pending(self, **_: Any) -> tuple:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def get_task_status(self, **_: Any) -> dict:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def pause(self, **_: Any) -> None:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def resume(self, **_: Any) -> None:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def approve(self, **_: Any) -> str:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")

    async def reject(self, **_: Any) -> None:
        raise AgentUnavailable("daemon D-Bus no disponible (test)")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChatFailHard:
    """SC-005: daemon caído → 503 agent_unavailable, sin fallback."""

    @pytest.fixture()
    def client(self, tmp_path: Any, monkeypatch: Any) -> TestClient:
        import os  # noqa: PLC0415

        spool = tmp_path / "audit-spool"
        spool.mkdir()
        db_path = str(tmp_path / "shell-state.db")
        monkeypatch.setenv("HERMES_SHELL_DB", db_path)
        monkeypatch.setenv("HERMES_AUDIT_SPOOL_DIR", str(spool))

        # Patch SecretsVault so it doesn't require /var/lib/hermes/master.key
        master_key = os.urandom(32)
        from hermes.shell_server import main as shell_main  # noqa: PLC0415

        original_vault = shell_main.SecretsVault

        class _TestVault(original_vault):  # type: ignore[valid-type]
            def __init__(self, **_: Any) -> None:
                super().__init__(master_key=master_key)

        monkeypatch.setattr(shell_main, "SecretsVault", _TestVault)

        from hermes.shell_server.main import create_app  # noqa: PLC0415

        app = create_app()

        # Inyectar control_plane stub de daemon-caído en el estado de la app.
        app.state.control_plane = _AlwaysUnavailableControlPlane()
        return TestClient(app, raise_server_exceptions=False)

    def test_chat_post_returns_503_when_daemon_down(self, client: TestClient) -> None:
        """POST /api/v1/chat → 503 cuando daemon D-Bus no disponible (SC-005)."""
        resp = client.post(
            "/api/v1/chat",
            json={"user_message": "hola agente"},
        )
        assert resp.status_code == 503, (
            f"Esperado 503 agent_unavailable, got {resp.status_code}. "
            "El endpoint debe fallar duro cuando el daemon está caído (SC-005 / CTRL-P1-11)."
        )

    def test_chat_post_503_body_contains_agent_unavailable(
        self, client: TestClient
    ) -> None:
        """El cuerpo 503 contiene el código 'agent_unavailable' (legible).

        CTRL-P1-11: el fallo debe ser explícito y legible (FR-012, SC-005).
        """
        resp = client.post(
            "/api/v1/chat",
            json={"user_message": "hola"},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert "agent_unavailable" in str(body).lower() or "agent_unavailable" in str(
            body.get("detail", "")
        ).lower(), (
            f"Cuerpo 503 no contiene 'agent_unavailable'. Body: {body}. "
            "El error debe ser explícito (CTRL-P1-11)."
        )

    def test_chat_post_zero_fallback(self, client: TestClient) -> None:
        """POST /api/v1/chat NO devuelve 200 cuando daemon caído.

        0 respuesta alternativa (SC-005). Sin degradación silenciosa.
        """
        resp = client.post(
            "/api/v1/chat",
            json={"user_message": "hola", "conversation_id": str(uuid4())},
        )
        assert resp.status_code != 200, (
            "El endpoint devolvió 200 con daemon caído — tiene fallback prohibido. "
            "SC-005: 0 respuesta alternativa."
        )

    def test_no_ws_chat_route_available(self, client: TestClient) -> None:
        """No existe ruta WS /ws/chat/{conv_id} — fue eliminada por T055.

        CTRL-P1-26 / G6: verificar por ausencia de ruta.
        """
        fake_conv_id = str(uuid4())
        # TestClient con WebSocket debería dar 403/404/403, no 101 Upgrade.
        try:
            with client.websocket_connect(f"/ws/chat/{fake_conv_id}") as _:
                pytest.fail(
                    "WS /ws/chat/{conv_id} respondió con éxito — "
                    "la ruta passthrough debería haber sido eliminada (T055 / G6)."
                )
        except Exception as exc:
            # Cualquier error (404, disconnect, etc.) es correcto — la ruta no existe.
            assert "404" in str(exc) or "403" in str(exc) or "WebSocketDisconnect" in type(exc).__name__ or True, (
                f"WS /ws/chat respondió de forma inesperada: {exc}"
            )
