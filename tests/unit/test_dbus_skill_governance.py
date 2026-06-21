"""D-Bus skill governance wiring (P0-1).

Tests that PromoteSkill / DeprecateSkill / SignComposioSkill:
  - Require authorized sender_uid (fail-closed).
  - Delegate to SkillGovernanceService (no logic duplication).
  - list_skills is read-only (no authZ).
  - FakeDbusInterface stubs are present for all new methods.
  - promote_skill verifies v2 signature before transitioning to AUTONOMOUS.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.shell_server.audit_api import init_schema
from hermes.shell_server.skills.skill_governance_service import SkillGovernanceService

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000

# Stable fake key for tests — returned by FakeVault.
_FAKE_SIGNING_KEY = b"\xCC" * 32


class _FakeVault:
    def derive_subkey(self, *, label: str) -> bytes:  # noqa: ARG002
        return _FAKE_SIGNING_KEY


def _fake_vault_patch():
    import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

    return patch.object(_mod, "SecretsVault", return_value=_FakeVault())


def _wiring(tmp_path: Path) -> tuple[DbusRuntimeServiceWiring, SkillGovernanceService]:
    db = tmp_path / "shell-state.db"
    init_schema(db)
    governance = SkillGovernanceService(db_path=db)
    wiring = DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({_OPERATOR_UID}),
        skill_governance=governance,
    )
    return wiring, governance


def _insert_skill(db: Path, *, state: str = "validated") -> str:
    """Insert a v2-signed skill_packages_view row; return package_id."""
    package_id = str(uuid4())
    skill_id = str(uuid4())
    signed_at = "2026-06-03T00:00:00+00:00"
    # Compute a valid v2 signature using the fake key.
    payload = f"{package_id}|{skill_id}|test-skill|1|{signed_at}|recorded"
    sig_hex = hmac.new(_FAKE_SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO skill_packages_view
          (package_id, skill_id, skill_name, version, state, surface_kinds, signed_at,
           signature_short, signing_method, signature_hex)
        VALUES (?, ?, ?, 1, ?, 'browser', ?, ?, 'v2', ?)
        """,
        (package_id, skill_id, "test-skill", state, signed_at, sig_hex[:12], sig_hex),
    )
    conn.commit()
    conn.close()
    return package_id


# ---------------------------------------------------------------------------
# list_skills — read-only, no authZ
# ---------------------------------------------------------------------------


class TestListSkillsReadOnly:
    def test_returns_empty_when_no_skills(self, tmp_path: Path) -> None:
        wiring, _ = _wiring(tmp_path)
        skills = wiring.list_skills()
        assert skills == []

    def test_returns_skills_without_authz(self, tmp_path: Path) -> None:
        db = tmp_path / "shell-state.db"
        init_schema(db)
        _insert_skill(db, state="validated")
        governance = SkillGovernanceService(db_path=db)
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_governance=governance,
        )
        skills = wiring.list_skills()
        assert len(skills) == 1
        assert skills[0]["state"] == "validated"

    def test_no_governance_returns_empty(self, tmp_path: Path) -> None:
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
        )
        assert wiring.list_skills() == []


# ---------------------------------------------------------------------------
# promote_skill — authorized mutator
# ---------------------------------------------------------------------------


class TestPromoteSkillWiring:
    def test_promote_requires_authorized_uid(self, tmp_path: Path) -> None:
        wiring, _ = _wiring(tmp_path)
        with pytest.raises(DbusAuthorizationError):
            asyncio.run(
                wiring.promote_skill(package_id=str(uuid4()), sender_uid=999)
            )

    def test_promote_happy_path(self, tmp_path: Path) -> None:
        db = tmp_path / "shell-state.db"
        init_schema(db)
        package_id = _insert_skill(db, state="validated")
        governance = SkillGovernanceService(db_path=db)
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_governance=governance,
        )
        with _fake_vault_patch():
            result = asyncio.run(
                wiring.promote_skill(package_id=package_id, sender_uid=_OPERATOR_UID)
            )
        assert result["state"] == "autonomous"
        assert result["package_id"] == package_id

    def test_promote_unauthorized_does_not_change_state(self, tmp_path: Path) -> None:
        db = tmp_path / "shell-state.db"
        init_schema(db)
        package_id = _insert_skill(db, state="validated")
        governance = SkillGovernanceService(db_path=db)
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_governance=governance,
        )
        with pytest.raises(DbusAuthorizationError):
            asyncio.run(
                wiring.promote_skill(package_id=package_id, sender_uid=42)
            )
        # State unchanged.
        skills = wiring.list_skills()
        assert skills[0]["state"] == "validated"


# ---------------------------------------------------------------------------
# deprecate_skill — authorized mutator
# ---------------------------------------------------------------------------


class TestDeprecateSkillWiring:
    def test_deprecate_requires_authorized_uid(self, tmp_path: Path) -> None:
        wiring, _ = _wiring(tmp_path)
        with pytest.raises(DbusAuthorizationError):
            asyncio.run(
                wiring.deprecate_skill(package_id=str(uuid4()), sender_uid=999)
            )

    def test_deprecate_happy_path(self, tmp_path: Path) -> None:
        db = tmp_path / "shell-state.db"
        init_schema(db)
        package_id = _insert_skill(db, state="validated")
        governance = SkillGovernanceService(db_path=db)
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_governance=governance,
        )
        result = asyncio.run(
            wiring.deprecate_skill(package_id=package_id, sender_uid=_OPERATOR_UID)
        )
        assert result["state"] == "deprecated"

    def test_deprecate_already_deprecated_raises_not_found(
        self, tmp_path: Path
    ) -> None:
        from hermes.shell_server.skills.skill_governance_service import SkillNotFound  # noqa: PLC0415

        db = tmp_path / "shell-state.db"
        init_schema(db)
        package_id = _insert_skill(db, state="deprecated")
        governance = SkillGovernanceService(db_path=db)
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_governance=governance,
        )
        with pytest.raises(SkillNotFound):
            asyncio.run(
                wiring.deprecate_skill(
                    package_id=package_id, sender_uid=_OPERATOR_UID
                )
            )


# ---------------------------------------------------------------------------
# sign_composio_skill — authorized mutator
# ---------------------------------------------------------------------------


class TestSignComposioSkillWiring:
    def test_sign_requires_authorized_uid(self, tmp_path: Path) -> None:
        wiring, _ = _wiring(tmp_path)
        draft = json.dumps(
            {
                "skill_name": "slack-notify",
                "toolkit_slug": "SLACK",
                "intent_text": "Send a Slack notification",
            }
        )
        with pytest.raises(DbusAuthorizationError):
            asyncio.run(
                wiring.sign_composio_skill(draft_json=draft, sender_uid=999)
            )

    def test_sign_happy_path_creates_skill(self, tmp_path: Path) -> None:
        wiring, _ = _wiring(tmp_path)
        draft = json.dumps(
            {
                "skill_name": "github-pr",
                "toolkit_slug": "GITHUB",
                "intent_text": "Open a PR with the changes",
            }
        )
        with _fake_vault_patch():
            result = asyncio.run(
                wiring.sign_composio_skill(draft_json=draft, sender_uid=_OPERATOR_UID)
            )
        assert result["state"] == "validated"
        assert result["skill_name"] == "github-pr"
        assert "package_id" in result

    def test_sign_invalid_json_raises_value_error(self, tmp_path: Path) -> None:
        wiring, _ = _wiring(tmp_path)
        with pytest.raises(ValueError, match="draft_json inválido"):
            asyncio.run(
                wiring.sign_composio_skill(
                    draft_json="{not valid json", sender_uid=_OPERATOR_UID
                )
            )

    def test_sign_no_governance_raises_runtime_error(self, tmp_path: Path) -> None:
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
        )
        draft = json.dumps(
            {"skill_name": "x", "toolkit_slug": "SLACK", "intent_text": "do it"}
        )
        with pytest.raises(RuntimeError, match="skill_governance"):
            asyncio.run(
                wiring.sign_composio_skill(
                    draft_json=draft, sender_uid=_OPERATOR_UID
                )
            )


# ---------------------------------------------------------------------------
# FakeDbusInterface stubs — client side
# ---------------------------------------------------------------------------


class TestFakeDbusInterfaceSkillStubs:
    @pytest.mark.asyncio
    async def test_list_skills_returns_empty_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        result = await fake.call_ListSkills()
        assert json.loads(result) == []

    @pytest.mark.asyncio
    async def test_promote_skill_returns_empty_object(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        result = await fake.call_PromoteSkill("some-package-id")
        assert json.loads(result) == {}

    @pytest.mark.asyncio
    async def test_deprecate_skill_returns_empty_object(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        result = await fake.call_DeprecateSkill("some-package-id")
        assert json.loads(result) == {}

    @pytest.mark.asyncio
    async def test_sign_composio_skill_echoes_draft(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        draft = json.dumps({"skill_name": "x"})
        result = await fake.call_SignComposioSkill(draft)
        assert result == draft


# ---------------------------------------------------------------------------
# DbusRuntimeClient high-level methods
# ---------------------------------------------------------------------------


class TestDbusRuntimeClientSkillMethods:
    @pytest.mark.asyncio
    async def test_list_skills_parses_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            DbusRuntimeClient,
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.list_skills()
        assert result == []

    @pytest.mark.asyncio
    async def test_promote_skill_parses_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            DbusRuntimeClient,
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.promote_skill("pkg-123")
        assert result == {}

    @pytest.mark.asyncio
    async def test_sign_composio_skill_sends_dict(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (  # noqa: PLC0415
            DbusRuntimeClient,
            FakeDbusInterface,
        )

        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        draft = {"skill_name": "x", "toolkit_slug": "SLACK", "intent_text": "do it"}
        result = await client.sign_composio_skill(draft)
        # FakeDbusInterface echoes the draft_json back; client parses it.
        assert result == draft
