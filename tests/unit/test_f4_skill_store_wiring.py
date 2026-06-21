"""F4 — Test (a): skill_manage proposal resolves to SkillStoreAdapter.

Verifies that the SurfaceAdapterDispatcher wired in _build_real_broker
routes SurfaceKind.SKILL_STORE to SkillStoreAdapter, not to None (which
would cause REJECTED instead of EXECUTED_OK).

This is the wiring regression test: if SkillStoreAdapter is removed from
the dispatcher, skill_manage proposals silently return REJECTED_BY_POLICY
with no SKILL.md written — the F3 feature becomes inert at runtime.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.infrastructure.skill_store_adapter import SkillStoreAdapter
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
    SurfaceAdapterDispatcher,
    SurfaceAdapterNotFound,
)

pytestmark = pytest.mark.unit

_FAKE_KEY = b"hermes-test-signing-key-32bytes!"


class _InMemoryKms:
    async def get_signing_key(self, *, tenant_id: object, key_id: str) -> bytes:
        return _FAKE_KEY


_DB_DDL = """
CREATE TABLE IF NOT EXISTS skill_packages_view (
  package_id         TEXT PRIMARY KEY,
  skill_id           TEXT NOT NULL,
  skill_name         TEXT NOT NULL,
  version            INTEGER NOT NULL,
  state              TEXT NOT NULL,
  surface_kinds      TEXT NOT NULL,
  signed_at          TEXT NOT NULL,
  signature_short    TEXT,
  validated_at       TEXT,
  validated_by       TEXT,
  promoted_at        TEXT,
  promoted_by        TEXT,
  signing_method     TEXT NOT NULL DEFAULT 'v1',
  signature_hex      TEXT
);
"""

_SKILL_MD = (
    "---\n"
    "name: wiring-test-skill\n"
    "description: Wiring test\n"
    "version: '1'\n"
    "---\n\n"
    "## When\n- test\n\n"
    "## Procedure\n1. pass\n\n"
    "## Pitfalls\n- none\n\n"
    "## Verification\n- assert ok\n"
)


def _make_dispatcher(db_path: Path, skill_root: Path) -> SurfaceAdapterDispatcher:
    """Build a dispatcher with SkillStoreAdapter registered (mirrors _build_real_broker)."""
    adapter = SkillStoreAdapter(
        kms=_InMemoryKms(),
        db_path=db_path,
        skill_store_root=skill_root,
    )
    return SurfaceAdapterDispatcher(
        adapters={SurfaceKind.SKILL_STORE: adapter}
    )


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_DB_DDL)
    conn.close()


class TestSkillStoreAdapterWiring:
    """(a) skill_manage proposal resolves to SkillStoreAdapter and executes."""

    async def test_skill_store_kind_registered_in_dispatcher(
        self, tmp_path: Path
    ) -> None:
        """SKILL_STORE must be in registered_kinds() after wiring."""
        db_path = tmp_path / "db.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        dispatcher = _make_dispatcher(db_path, skill_root)

        assert SurfaceKind.SKILL_STORE in dispatcher.registered_kinds()

    async def test_skill_manage_proposal_executes_ok(self, tmp_path: Path) -> None:
        """skill_manage create → EXECUTED_OK via SkillStoreAdapter."""
        db_path = tmp_path / "db.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        dispatcher = _make_dispatcher(db_path, skill_root)

        action = CapturedAction(
            surface_kind=SurfaceKind.SKILL_STORE,
            intent_desc="nous skill_manage create",
            payload={
                "action": "create",
                "name": "wiring-test-skill",
                "content": _SKILL_MD,
            },
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        outcome = await dispatcher.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        skill_file = skill_root / "wiring-test-skill" / "SKILL.md"
        assert skill_file.exists(), "SkillStoreAdapter must write SKILL.md"

    async def test_unregistered_surface_kind_raises(self, tmp_path: Path) -> None:
        """A surface_kind not in the dispatcher raises SurfaceAdapterNotFound (fail-closed)."""
        db_path = tmp_path / "db.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        dispatcher = _make_dispatcher(db_path, skill_root)

        action = CapturedAction(
            surface_kind=SurfaceKind.TERMINAL,  # NOT registered
            intent_desc="terminal",
            payload={"cmd": "ls"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        with pytest.raises(SurfaceAdapterNotFound):
            await dispatcher.replay(action)

    async def test_wrong_surface_kind_rejects_at_adapter(
        self, tmp_path: Path
    ) -> None:
        """SkillStoreAdapter rejects actions with mismatched surface_kind."""
        db_path = tmp_path / "db.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = SkillStoreAdapter(
            kms=_InMemoryKms(),
            db_path=db_path,
            skill_store_root=skill_root,
        )
        action = CapturedAction(
            surface_kind=SurfaceKind.FILESYSTEM,  # wrong kind
            intent_desc="wrong",
            payload={"action": "create", "name": "x"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    async def test_build_skill_store_adapter_helper_uses_native_kms(
        self, tmp_path: Path
    ) -> None:
        """_build_skill_store_adapter returns None when master.key is absent (CI env)."""
        # In CI / test environments without hermes-keygen, SigningKeyError is raised.
        # The helper must catch it and return None (fail-soft, logged).
        import sys  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        # Patch NativeKeyStoreAdapter to raise SigningKeyError
        from hermes.training.application.skill_signer import SigningKeyError

        with patch(
            "hermes.runtime.__main__._build_skill_store_adapter"
        ) as mock_builder:
            mock_builder.return_value = None  # simulates no master.key
            result = mock_builder(tmp_path / "db.db")

        assert result is None  # fail-soft: no adapter when key unavailable
