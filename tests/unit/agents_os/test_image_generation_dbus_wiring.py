"""Image generation (FAL.ai) key D-Bus wiring — mirrors set_web_search_api_key.

Covers DbusRuntimeServiceWiring.{set,delete}_image_generation_api_key and
get_image_generation_status:
  - Unauthorized sender_uid raises DbusAuthorizationError (never persists).
  - An empty api_key is rejected, never written.
  - A valid key is written to HERMES_HOME/.env AND injected into the live
    os.environ (FAL_KEY) so check_image_generation_requirements() sees it
    without a daemon restart.
  - Deleting clears both the live env and the persisted value.
  - The key value itself never appears in any log record.
  - The three verbs are exported on the dbus-fast ServiceInterface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999
_SECRET_KEY = "fal-super-secret-key-should-never-leak"


class _NullApprovalGate:
    """Unused stub — image-generation verbs never touch the approval gate;
    only present to satisfy DbusRuntimeServiceWiring's constructor."""

    async def register_pending(self, **_kwargs: object) -> None: ...
    async def approve(self, **_kwargs: object) -> str:
        return ""
    async def reject(self, **_kwargs: object) -> None: ...
    async def verify_token(self, **_kwargs: object) -> bool:
        return False
    async def approved_token_for(self, *_args: object) -> str | None:
        return None


def _make_wiring() -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
    )


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HERMES_HOME/.env and the live FAL_KEY env var per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("FAL_KEY", raising=False)
    return tmp_path


def _env_file(hermes_home: Path) -> Path:
    return hermes_home / ".env"


class TestUnauthorized:
    @pytest.mark.asyncio
    async def test_set_unauthorized_uid_raises(self, _hermes_home: Path) -> None:
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.set_image_generation_api_key(
                api_key=_SECRET_KEY, sender_uid=_UNAUTHORIZED_UID
            )
        assert not _env_file(_hermes_home).exists()
        import os
        assert "FAL_KEY" not in os.environ

    @pytest.mark.asyncio
    async def test_delete_unauthorized_uid_raises(self, _hermes_home: Path) -> None:
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.delete_image_generation_api_key(sender_uid=_UNAUTHORIZED_UID)


class TestSetKey:
    @pytest.mark.asyncio
    async def test_empty_key_rejected(self, _hermes_home: Path) -> None:
        wiring = _make_wiring()
        result = await wiring.set_image_generation_api_key(
            api_key="   ", sender_uid=_OPERATOR_UID
        )
        assert result["ok"] is False
        assert not _env_file(_hermes_home).exists()

    @pytest.mark.asyncio
    async def test_valid_key_persists_and_exports_live_env(
        self, _hermes_home: Path
    ) -> None:
        import os

        wiring = _make_wiring()
        result = await wiring.set_image_generation_api_key(
            api_key=_SECRET_KEY, sender_uid=_OPERATOR_UID
        )
        assert result == {"ok": True, "provider": "fal", "configured": True}
        assert os.environ["FAL_KEY"] == _SECRET_KEY
        assert f"FAL_KEY={_SECRET_KEY}" in _env_file(_hermes_home).read_text()

    @pytest.mark.asyncio
    async def test_key_never_appears_in_any_log_record(
        self, _hermes_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        wiring = _make_wiring()
        with caplog.at_level("DEBUG"):
            await wiring.set_image_generation_api_key(
                api_key=_SECRET_KEY, sender_uid=_OPERATOR_UID
            )
        leaked = [r.getMessage() for r in caplog.records if _SECRET_KEY in r.getMessage()]
        assert leaked == []


class TestGetStatus:
    def test_has_key_false_when_unset(self, _hermes_home: Path) -> None:
        wiring = _make_wiring()
        status = wiring.get_image_generation_status()
        assert status == {"provider": "fal", "has_key": False, "model": None}

    @pytest.mark.asyncio
    async def test_has_key_true_after_set(self, _hermes_home: Path) -> None:
        wiring = _make_wiring()
        await wiring.set_image_generation_api_key(
            api_key=_SECRET_KEY, sender_uid=_OPERATOR_UID
        )
        status = wiring.get_image_generation_status()
        assert status["provider"] == "fal"
        assert status["has_key"] is True

    def test_status_never_echoes_the_key(self, _hermes_home: Path) -> None:
        wiring = _make_wiring()
        status = wiring.get_image_generation_status()
        assert "api_key" not in status
        assert "key" not in status


class TestDeleteKey:
    @pytest.mark.asyncio
    async def test_delete_clears_live_env_and_status(self, _hermes_home: Path) -> None:
        import os

        wiring = _make_wiring()
        await wiring.set_image_generation_api_key(
            api_key=_SECRET_KEY, sender_uid=_OPERATOR_UID
        )
        result = await wiring.delete_image_generation_api_key(sender_uid=_OPERATOR_UID)
        assert result == {"ok": True, "provider": "fal", "configured": False}
        assert "FAL_KEY" not in os.environ
        assert wiring.get_image_generation_status()["has_key"] is False


class TestDbusExport:
    def test_verbs_exported_on_service_interface(self) -> None:
        pytest.importorskip("dbus_fast")
        from dbus_fast.proxy_object import BaseProxyInterface
        from dbus_fast.service import ServiceInterface

        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            Runtime1ServiceInterface,
        )

        class _StubWiring:
            pass

        iface = Runtime1ServiceInterface(wiring=_StubWiring())  # type: ignore[arg-type]
        methods = ServiceInterface._get_methods(iface)
        exported = {BaseProxyInterface._to_snake_case(m.name) for m in methods}
        assert "set_image_generation_api_key" in exported
        assert "delete_image_generation_api_key" in exported
        assert "get_image_generation_status" in exported
