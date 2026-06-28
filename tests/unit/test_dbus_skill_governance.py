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
    # SkillGovernanceService creates skill_packages_view schema on init
    governance = SkillGovernanceService(db_path=db)
    wiring = DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({_OPERATOR_UID}),
        skill_governance=governance,
    )
    return wiring, governance


def _insert_skill(db: Path, *, state: str = "validated") -> str:
    """Insert a v2-signed skill_packages_view row; return package_id.

    SkillGovernanceService.init ensures the schema exists before insertion.
    """
    # Ensure governance schema exists (skill_packages_view).
    SkillGovernanceService(db_path=db)

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


def _write_skill_md(skills_root: Path, name: str, state: str = "native") -> None:
    """Write a minimal SKILL.md into skills_root/<name>/SKILL.md."""
    import yaml as _yaml

    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = {
        "name": name,
        "description": f"Test skill {name}",
        "version": "1",
        "metadata": {"state": state, "signing_method": "v2" if state == "validated" else "none"},
    }
    content = f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- test\n\n## Procedure\n1. step\n"
    (skill_dir / "SKILL.md").write_text(content)


class TestListSkillsReadOnly:
    def test_returns_empty_when_no_skills_dir(self, tmp_path: Path) -> None:
        """list_skills_native() returns [] when skills_root does not exist."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _list_native_skills_primary,
        )
        skills = _list_native_skills_primary(skills_root=tmp_path / "no-such-dir")
        assert skills == []

    def test_returns_native_skills_from_disk(self, tmp_path: Path) -> None:
        """list_skills_native() finds skills written only to disk (BUG 3 regression)."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _list_native_skills_primary,
        )
        skills_root = tmp_path / "skills"
        _write_skill_md(skills_root, "agent-created-skill", state="native")

        skills = _list_native_skills_primary(skills_root=skills_root)
        assert len(skills) == 1
        assert skills[0]["skill_name"] == "agent-created-skill"
        assert skills[0]["state"] == "native"

    def test_no_governance_returns_empty_composio(self, tmp_path: Path) -> None:
        """Without governance, composio list is empty; native skills still load."""
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
        )
        # list_skills calls list_skills_native() — returns [] when no HERMES_HOME set
        skills = wiring.list_skills()
        assert isinstance(skills, list)


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
        # State unchanged — verify via the governance service DB directly.
        db_skills = governance.list_skills()
        assert db_skills[0]["state"] == "validated"


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


# ---------------------------------------------------------------------------
# create_skill_from_text — wiring delegates to SkillStoreAdapter (new verb)
# ---------------------------------------------------------------------------


class _FakeSkillStoreAdapter:
    """Fake SkillStoreAdapter: captures the CapturedAction and returns a canned outcome."""

    def __init__(self) -> None:
        self.calls: list = []
        self._result = {
            "package_id": "pkg-aaa",
            "skill_id": "skl-bbb",
            "name": "test-skill",
            "state": "validated",
            "signing_method": "v2",
        }

    @property
    def surface_kind(self):
        from hermes.agents_os.domain.surface_kind import SurfaceKind  # noqa: PLC0415
        return SurfaceKind.SKILL_STORE

    async def replay(self, action, *, hitl_approval_token=None, consent_token=None):
        from hermes.agents_os.domain.ports.surface_adapter_port import ReplayOutcome  # noqa: PLC0415
        self.calls.append(action)
        return ReplayOutcome.ok(action.action_id, result=dict(self._result))


_SKILL_MD = """\
---
description: Skill de prueba para el wiring D-Bus.
---
# Test Skill

## Objetivo
Verificar que create_skill_from_text construye la CapturedAction correcta.

## Cuándo usarla
En tests unitarios.

## Pasos
1. Llamar al método.

## Herramientas
ninguna

## Límites y seguridad
Solo en tests.
"""


class TestCreateSkillFromTextWiring:
    def test_requires_authorized_uid(self, tmp_path: Path) -> None:
        """Unauthorized UID raises DbusAuthorizationError before touching the adapter."""
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_store_adapter=_FakeSkillStoreAdapter(),
        )
        with pytest.raises(DbusAuthorizationError):
            asyncio.run(
                wiring.create_skill_from_text(
                    name="my-skill", skill_md=_SKILL_MD, sender_uid=999
                )
            )

    def test_no_adapter_raises_runtime_error(self) -> None:
        """When skill_store_adapter is None the method raises RuntimeError (fail-closed)."""
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_store_adapter=None,
        )
        with pytest.raises(RuntimeError, match="skill_store_adapter"):
            asyncio.run(
                wiring.create_skill_from_text(
                    name="my-skill", skill_md=_SKILL_MD, sender_uid=_OPERATOR_UID
                )
            )

    def test_happy_path_builds_correct_captured_action(self) -> None:
        """create_skill_from_text constructs a CREATE CapturedAction and delegates."""
        from hermes.agents_os.domain.surface_kind import SurfaceKind  # noqa: PLC0415

        fake_adapter = _FakeSkillStoreAdapter()
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_store_adapter=fake_adapter,
        )
        result = asyncio.run(
            wiring.create_skill_from_text(
                name="my-skill", skill_md=_SKILL_MD, sender_uid=_OPERATOR_UID
            )
        )

        # Adapter was called exactly once.
        assert len(fake_adapter.calls) == 1
        action = fake_adapter.calls[0]

        # CapturedAction has the correct surface and payload.
        assert action.surface_kind == SurfaceKind.SKILL_STORE
        assert action.payload["action"] == "create"
        assert action.payload["name"] == "my-skill"
        assert action.payload["content"] == _SKILL_MD

        # Return dict contains expected keys.
        assert result["package_id"] == "pkg-aaa"
        assert result["skill_id"] == "skl-bbb"
        assert result["skill_name"] == "test-skill"
        assert result["version"] == 1

    def test_adapter_rejection_raises_runtime_error(self) -> None:
        """When the adapter returns REJECTED_BY_POLICY, the wiring raises RuntimeError."""
        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            CapturedAction,
            ReplayOutcome,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind  # noqa: PLC0415

        class _BlockingAdapter:
            surface_kind = SurfaceKind.SKILL_STORE

            async def replay(self, action, **_):
                return ReplayOutcome.rejected_by_policy(
                    action.action_id, reason="trojan content detected"
                )

        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({_OPERATOR_UID}),
            skill_store_adapter=_BlockingAdapter(),
        )
        with pytest.raises(RuntimeError, match="rechazado por SkillStoreAdapter"):
            asyncio.run(
                wiring.create_skill_from_text(
                    name="evil-skill", skill_md=_SKILL_MD, sender_uid=_OPERATOR_UID
                )
            )
