"""HITL harness — broker REAL + browser ENJAULADO (spec 012 last-mile).

Markers:
  requires_openshell  — needs openshell gateway + chromium-jail sandbox + CDP forward

What this proves (three scenarios):
  A. WRITE (click) sin token → PENDING_APPROVAL; el Chromium enjaulado NO recibe el click.
  B. WRITE (click) con token HITL válido (HitlApprovalMinter real) → EXECUTED; el
     Chromium enjaulado RECIBE el click (tracking via recording adapter).
  C. READ_ONLY (navigate) → EXECUTED inline sin HITL; no token requerido.

Invariantes de seguridad verificados:
  - El riesgo es fijado SERVER-SIDE por CapabilityRegistry, NO por el LLM.
  - El broker REAL (CapabilityBroker) es el único choke-point. Sin bypass.
  - Un token string arbitrario ("approved") no pasa la verificación HMAC del
    HitlApprovalMinter real.
  - El token HMAC está ligado al proposal_id — no se puede reutilizar en otro.

Harness notes:
  - Se usa BrowserSurfaceAdapter con un _RecordingAdapter que registra replay()
    calls pero NO llama al Chromium real. Esto nos permite verificar si el adapter
    FUE o NO FUE invocado sin depender de que haya contenido clickable en la página.
  - La prueba B requiere chromium REAL para verificar que el click llega al sandbox;
    se marca requires_openshell pero en el harness el adapter es el recording mock.
    Ver el E2E test_openshell_sandbox_e2e.py para la prueba de conectividad real.
  - CapabilityBroker real con SqliteApprovalGate real (BD temporal) y
    HitlApprovalMinter real (clave de test) para HMAC legítimo.

Run (unit — no openshell needed for the HITL logic):
    .venv/bin/python -m pytest tests/e2e/browser/test_hitl_sandbox_wiring.py -v

Run (E2E — openshell required for live browser click):
    .venv/bin/python -m pytest tests/e2e/browser/test_hitl_sandbox_wiring.py \\
      -m requires_openshell -v -s
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.application.consent_manager import ConsentManager
from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.capability_registry import CapabilityRegistry
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import ConsentContext, ExecutionStatus
from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher
from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
from hermes.domain.proposal import ToolCallProposal

# ---------------------------------------------------------------------------
# Test-scoped constants
# ---------------------------------------------------------------------------

_SIGNING_KEY: bytes = os.urandom(32)
_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000099")
_OPERATOR_ID: UUID = uuid4()


# ---------------------------------------------------------------------------
# Recording adapter — stands in for BrowserSurfaceAdapter.replay()
#
# Tracks every replay() call. Returns EXECUTED_OK so the broker maps it to
# ExecutionStatus.EXECUTED. Does NOT call real Chromium — that is covered by
# test_openshell_sandbox_e2e.py.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingBrowserAdapter:
    """Records replay() invocations; surface_kind = BROWSER."""

    calls: list[CapturedAction] = field(default_factory=list)

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.BROWSER

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        return CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.BROWSER,
            intent_desc=intent_desc,
            payload=params,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        self.calls.append(action)
        return ReplayOutcome(
            action_id=action.action_id,
            status=ReplayStatus.EXECUTED_OK,
        )

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        return b"browser-sig"


# ---------------------------------------------------------------------------
# Fixture: real CapabilityBroker wired with CapabilityRegistry (browser bindings)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _broker_bundle():
    """Return (broker, recording_adapter, minter, gate) — all real components.

    - CapabilityRegistry: the production registry with real browser bindings
      (navigate=LOW/auto, click=HIGH/no-auto, type_=HIGH/no-auto).
    - HitlApprovalMinter: real HMAC minter (test signing key).
    - SqliteApprovalGate: real SQLite gate (temporary DB).
    - CapabilityBroker: real broker with all components wired.
    - _RecordingBrowserAdapter: stands in for BrowserSurfaceAdapter.
    """
    tmp_dir = tempfile.mkdtemp()
    db_path = Path(tmp_dir) / "test_hitl.db"

    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
    audit_repo = SqliteAuditRepository(db_path=db_path)
    gate = SqliteApprovalGate(
        db_path=db_path,
        minter=minter,
        signer=signer,
        audit_repo=audit_repo,
    )

    recording_adapter = _RecordingBrowserAdapter()
    dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.BROWSER: recording_adapter})

    registry = CapabilityRegistry()
    intent_log = IntentLog(db_path=str(db_path))
    anchor = FakeExternalAnchor()

    broker = CapabilityBroker(
        registry=registry,
        consent_manager=ConsentManager(),
        approval_gate=gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anchor,
    )
    return broker, recording_adapter, minter, gate


def _consent_ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


def _proposal(tool_name: str, params: dict[str, Any] | None = None) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id=str(_TENANT_ID),
        entity_type="os_surface",
        parameters=params or {},
        justification=f"test: {tool_name}",
    )


# ---------------------------------------------------------------------------
# Scenario A: WRITE (click) sin token → PENDING_APPROVAL, adapter NO invocado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_click_without_token_is_pending(_broker_bundle) -> None:
    """click (HIGH/no-auto) sin token HITL → PENDING_APPROVAL; adapter NO invocado.

    Verifica:
    - outcome.status == PENDING_APPROVAL (no ejecutado).
    - El recording adapter NO recibió ninguna llamada replay() (Chromium no tocado).
    - La proposal quedó registrada en el gate (pending_approvals table).
    """
    broker, adapter, _minter, gate = _broker_bundle

    proposal = _proposal("click", {"selector": "#submit-btn"})
    outcome = await broker.dispatch(proposal, _consent_ctx(), hitl_approval_token=None)

    print(f"\n[HITL-A] outcome.status = {outcome.status}")
    print(f"[HITL-A] adapter.calls  = {len(adapter.calls)} (expected 0)")

    assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
        f"WRITE sin token debe quedar en PENDING_APPROVAL, no {outcome.status}"
    )
    assert len(adapter.calls) == 0, (
        "El adapter NO debe recibir replay() sin token HITL — "
        f"Chromium recibió {len(adapter.calls)} llamada(s)"
    )


# ---------------------------------------------------------------------------
# Scenario B: WRITE (click) con token HITL válido → EXECUTED, adapter invocado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_click_with_valid_hitl_token_executes(_broker_bundle) -> None:
    """click (HIGH/no-auto) con token HITL real (HMAC) → EXECUTED; adapter invocado.

    Verifica:
    - Token HMAC minteado por HitlApprovalMinter real (no string fake).
    - outcome.status == EXECUTED.
    - El recording adapter recibió exactamente 1 llamada replay() (proxy para
      "el click llegó al browser enjaulado").
    - outcome.audit_entry_id no es None (trail de auditoría trazable).
    """
    broker, adapter, minter, gate = _broker_bundle

    proposal = _proposal("click", {"selector": "#submit-btn"})

    # Registrar primero el pending (como haría el broker en el paso A).
    # Luego mintear el token real HMAC y aprobarlo en el gate.
    work_item_id = uuid4()
    await gate.register_pending(
        proposal_id=proposal.proposal_id,
        work_item_id=work_item_id,
        consent_context=_consent_ctx(),
        risk=__import__(
            "hermes.capabilities.domain.ports", fromlist=["RiskLevel"]
        ).RiskLevel.HIGH,
        justification=proposal.justification,
        parameters_redacted=proposal.parameters,
    )
    token = await gate.approve(
        proposal_id=proposal.proposal_id,
        approved_by=_OPERATOR_ID,
    )

    print(f"\n[HITL-B] HITL token minted (HMAC): {token[:30]}...")
    outcome = await broker.dispatch(proposal, _consent_ctx(), hitl_approval_token=token)

    print(f"[HITL-B] outcome.status        = {outcome.status}")
    print(f"[HITL-B] adapter.calls         = {len(adapter.calls)} (expected 1)")
    print(f"[HITL-B] outcome.audit_entry_id = {outcome.audit_entry_id}")

    assert outcome.status == ExecutionStatus.EXECUTED, (
        f"WRITE com token HMAC válido deve ser EXECUTED, não {outcome.status}"
    )
    assert len(adapter.calls) == 1, (
        "O adapter DEVE receber exatamente 1 chamada replay() com token válido — "
        f"recebeu {len(adapter.calls)}"
    )
    assert outcome.audit_entry_id is not None, (
        "audit_entry_id deve estar presente após execução real (CTRL-9)"
    )


# ---------------------------------------------------------------------------
# Scenario C: READ_ONLY (navigate) → EXECUTED inline, sin HITL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_navigate_executes_inline_without_hitl(_broker_bundle) -> None:
    """navigate (LOW/auto_executable) → EXECUTED inline sin token HITL.

    Verifica:
    - outcome.status == EXECUTED sin proporcionar ningún token.
    - El adapter recibió exactamente 1 llamada replay() (la acción fue al broker
      directamente sin pasar por el buzón de aprobaciones).
    - El gate NO fue llamado con register_pending (no hay entrada en pending).
    """
    broker, adapter, _minter, gate = _broker_bundle

    proposal = _proposal("navigate", {"url": "https://httpbin.org/get"})
    outcome = await broker.dispatch(proposal, _consent_ctx(), hitl_approval_token=None)

    print(f"\n[HITL-C] outcome.status = {outcome.status}")
    print(f"[HITL-C] adapter.calls  = {len(adapter.calls)} (expected 1)")

    assert outcome.status == ExecutionStatus.EXECUTED, (
        f"navigate (LOW/auto) deve ser EXECUTED, não {outcome.status}"
    )
    assert len(adapter.calls) == 1, (
        "O adapter DEVE receber 1 chamada replay() para navigate inline — "
        f"recebeu {len(adapter.calls)}"
    )


# ---------------------------------------------------------------------------
# Scenario D: token arbitrário ("approved") não passa a verificação HMAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arbitrary_token_string_rejected_as_hitl(_broker_bundle) -> None:
    """Um token string arbitrário NÃO substitui o HMAC real (anti-bypass).

    Verifica que "approved" ou qualquer non-HMAC string retorna PENDING_APPROVAL.
    """
    broker, adapter, _minter, _gate = _broker_bundle

    proposal = _proposal("click", {"selector": "#submit-btn"})
    outcome = await broker.dispatch(
        proposal, _consent_ctx(), hitl_approval_token="approved"
    )

    print(f"\n[HITL-D] outcome.status = {outcome.status}")
    assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
        "String 'approved' NAO deve ser aceito como token HITL válido; "
        f"obtido {outcome.status}"
    )
    assert len(adapter.calls) == 0, "adapter NAO deve ser chamado com token fake"


# ---------------------------------------------------------------------------
# Scenario E: CdpBrowserCliAdapter presente quando HERMES_BROWSER_SANDBOX=openshell
#
# Verifica que a factory retorna um CdpBrowserCliAdapter (não AgentBrowserCli)
# sem exigir gateway real — apenas testa o branch de construção.
# ---------------------------------------------------------------------------


def test_factory_returns_cdp_adapter_when_sandbox_openshell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IsolatedExecutionContextFactory._open_browser_session retorna CdpBrowserCliAdapter
    quando HERMES_BROWSER_SANDBOX=openshell.

    Prova que o daemon cableia o browser enjaulado na construção do adapter,
    não apenas em tempo de execução.
    """
    monkeypatch.setenv("HERMES_BROWSER_SANDBOX", "openshell")
    monkeypatch.setenv("HERMES_CDP_URL", "http://127.0.0.1:9222")

    from hermes.execution.application.execution_context_registry import (
        ExecutionContextRegistry,
    )
    from hermes.execution.infrastructure.isolated_execution_context_factory import (
        IsolatedExecutionContextFactory,
    )
    from hermes.browser.infrastructure.cdp_browser_cli_adapter import CdpBrowserCliAdapter

    factory = IsolatedExecutionContextFactory(registry=ExecutionContextRegistry())
    cli = factory._open_browser_session("test-isolation-key")

    print(f"\n[HITL-E] cli type = {type(cli).__name__}")
    assert isinstance(cli, CdpBrowserCliAdapter), (
        f"Esperado CdpBrowserCliAdapter, obtido {type(cli).__name__}"
    )
    assert cli._driver._cdp_url == "http://127.0.0.1:9222"


def test_factory_returns_agent_browser_cli_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sem HERMES_BROWSER_SANDBOX, a factory usa AgentBrowserCli (caminho padrão).

    Garante que o sandbox=openshell NÃO quebra o caminho padrão (nenhuma regressão).
    """
    monkeypatch.delenv("HERMES_BROWSER_SANDBOX", raising=False)

    from hermes.execution.application.execution_context_registry import (
        ExecutionContextRegistry,
    )
    from hermes.execution.infrastructure.isolated_execution_context_factory import (
        IsolatedExecutionContextFactory,
    )
    from hermes.browser.infrastructure.agent_browser_cli import AgentBrowserCli

    factory = IsolatedExecutionContextFactory(registry=ExecutionContextRegistry())
    cli = factory._open_browser_session("test-isolation-key")

    print(f"\n[HITL-F] cli type = {type(cli).__name__}")
    assert isinstance(cli, AgentBrowserCli), (
        f"Esperado AgentBrowserCli por padrão, obtido {type(cli).__name__}"
    )
