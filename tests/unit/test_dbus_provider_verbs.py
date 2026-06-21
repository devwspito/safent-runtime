"""GATE 0 / M1 🔒 — Regresión de EJECUCIÓN de los verbos de provider del daemon.

Los tests de contrato (test_dbus_runtime1_contract) sólo verifican firmas D-Bus;
NO ejecutaban los handlers. Por eso un `json.loads` con `json` sin importar a nivel
de módulo pasó verde y reventó en la VM con NameError. Esta suite EJECUTA cada verbo
contra un SQLiteProviderRepository real (DB temporal + SecretsVault efímero) para que
cualquier NameError / firma rota / lógica de _provider_to_dict se detecte en CI, no
en un bake.

Cubre add → list → set_active → get_active → update → delete + authZ (sender_uid).
NO cubre test_provider (requiere runtime Nous + red) — eso se valida en la VM.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999


class _NullApprovalGate:
    """ApprovalGatePort no usado por los verbos de provider (sólo para construir)."""

    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring(tmp_path: Path) -> DbusRuntimeServiceWiring:
    vault = SecretsVault(master_key=os.urandom(32))
    repo = SQLiteProviderRepository(db_path=tmp_path / "providers.db", vault=vault)
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        provider_repo=repo,
    )


def _draft(**over: object) -> str:
    import json

    base = {
        "kind": "openai",
        "alias": "OpenAI",
        "default_model": "gpt-5.4-nano",
        "api_key": "sk-test-not-real",
        "set_active": False,
    }
    base.update(over)
    return json.dumps(base)


def test_add_provider_executes_and_persists(tmp_path: Path) -> None:
    """El bug del NameError(json): add_provider DEBE parsear el draft y guardar."""
    wiring = _make_wiring(tmp_path)
    saved = wiring.add_provider(draft_json=_draft(), sender_uid=_OPERATOR_UID)
    assert saved["alias"] == "OpenAI"
    assert saved["kind"] == "openai"
    assert saved["default_model"] == "gpt-5.4-nano"
    assert saved["has_api_key"] is True
    assert saved["is_active"] is False
    assert saved["provider_id"]
    # la api_key NO se devuelve en claro
    assert "sk-test-not-real" not in str(saved)

    listed = wiring.list_providers()
    assert len(listed) == 1
    assert listed[0]["provider_id"] == saved["provider_id"]


def test_add_with_set_active_activates(tmp_path: Path) -> None:
    wiring = _make_wiring(tmp_path)
    saved = wiring.add_provider(draft_json=_draft(set_active=True), sender_uid=_OPERATOR_UID)
    assert saved["is_active"] is True
    active = wiring.get_active_provider()
    assert active["provider_id"] == saved["provider_id"]


def test_set_active_then_get_active(tmp_path: Path) -> None:
    wiring = _make_wiring(tmp_path)
    saved = wiring.add_provider(draft_json=_draft(), sender_uid=_OPERATOR_UID)
    assert wiring.get_active_provider() == {}  # nada activo aún
    activated = wiring.set_active_provider(
        provider_id=saved["provider_id"], sender_uid=_OPERATOR_UID
    )
    assert activated["is_active"] is True
    assert wiring.get_active_provider()["provider_id"] == saved["provider_id"]


def test_update_provider_changes_model(tmp_path: Path) -> None:
    """update_provider también usaba json.loads — cubre la misma clase de bug."""
    wiring = _make_wiring(tmp_path)
    saved = wiring.add_provider(draft_json=_draft(), sender_uid=_OPERATOR_UID)
    updated = wiring.update_provider(
        provider_id=saved["provider_id"],
        draft_json=_draft(default_model="gpt-5.4-mini"),
        sender_uid=_OPERATOR_UID,
    )
    assert updated["default_model"] == "gpt-5.4-mini"


def test_delete_provider(tmp_path: Path) -> None:
    wiring = _make_wiring(tmp_path)
    saved = wiring.add_provider(draft_json=_draft(), sender_uid=_OPERATOR_UID)
    assert wiring.delete_provider(
        provider_id=saved["provider_id"], sender_uid=_OPERATOR_UID
    ) is True
    assert wiring.list_providers() == []


def test_mutators_deny_unauthorized_uid(tmp_path: Path) -> None:
    """Fail-closed: un uid fuera de authorized_uids no puede mutar (CWE-862)."""
    wiring = _make_wiring(tmp_path)
    with pytest.raises(DbusAuthorizationError):
        wiring.add_provider(draft_json=_draft(), sender_uid=_UNAUTHORIZED_UID)


def test_reads_do_not_require_authorization(tmp_path: Path) -> None:
    """list/get_active son read-only: no exigen authZ (supervisión)."""
    wiring = _make_wiring(tmp_path)
    assert wiring.list_providers() == []
    assert wiring.get_active_provider() == {}
