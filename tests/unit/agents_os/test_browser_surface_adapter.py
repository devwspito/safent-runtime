"""Unit tests — BrowserSurfaceAdapter (feature 006 / Phase 2b).

Covers:
  - Session reuse: N replays for the same work_item_id → ONE session opened.
  - Session isolation: distinct work_item_ids → distinct sessions.
  - close_task: idempotent, calls factory.close exactly once.
  - close_task: releases even when a verb raised mid-task.
  - Hybrid policy — WRITE on non-approved site → rejected_by_policy.
  - Hybrid policy — WRITE on approved site → proceeds.
  - Hybrid policy — READ / navigate on open web → allowed (no allowlist check).
  - Surface mismatch → rejected_by_policy (no session mutation).
  - Unknown op → rejected_by_policy.
  - Missing work_item_id → rejected_by_policy.
  - INV-BROWSER-ADMISSION: HIGH-risk binding + no HITL token → PENDING_APPROVAL
    (factory.open NOT called — broker gates before adapter).
  - Regression: existing stateless adapters unaffected by new work_item_id field.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.browser_surface_adapter import (
    BrowserSurfaceAdapter,
    _host_is_approved,
)
from hermes.browser.application.browser_session_registry import (
    BrowserSessionRegistry,
    BrowserTaskSession,
)
from hermes.browser.infrastructure.agent_browser_cli import AgentBrowserCli
from hermes.execution.domain.ports import ExecutionContextId, InputSurfaceKind

pytestmark = pytest.mark.unit

_TENANT = uuid4()


# ---------------------------------------------------------------------------
# Fake browser CLI — records calls, no subprocess
# ---------------------------------------------------------------------------


@dataclass
class _FakeCli:
    """Minimal AgentBrowserCli stand-in for unit tests."""

    _current_url: str = "https://approved.example.com/page"
    navigate_calls: list[str] = field(default_factory=list)
    snapshot_calls: int = 0
    click_calls: list[str] = field(default_factory=list)
    type_calls: list[tuple[str, str]] = field(default_factory=list)
    close_called: bool = False
    raise_on_navigate: Exception | None = None

    async def navigate(self, url: str) -> None:
        if self.raise_on_navigate:
            raise self.raise_on_navigate
        self.navigate_calls.append(url)
        self._current_url = url

    async def snapshot(self) -> str:
        self.snapshot_calls += 1
        return f"URL: {self._current_url}\n@e1 [button] Submit"

    async def click(self, ref: str) -> None:
        self.click_calls.append(ref)

    async def type_(self, ref: str, text: str) -> None:
        self.type_calls.append((ref, text))

    async def current_url(self) -> str:
        return self._current_url

    async def close(self) -> None:
        self.close_called = True


# ---------------------------------------------------------------------------
# Fake factory — tracks open/close without touching OS resources
# ---------------------------------------------------------------------------


@dataclass
class _FakeContext:
    isolation_key: str


@dataclass
class _FakeFactory:
    """Records open/close calls; hands back a fake ExecutionContext."""

    opened: list[tuple[ExecutionContextId, InputSurfaceKind, str]] = field(
        default_factory=list
    )
    closed: list[UUID] = field(default_factory=list)
    cli_by_seed: dict[str, _FakeCli] = field(default_factory=dict)

    def _cli_for(self, seed: str) -> _FakeCli:
        if seed not in self.cli_by_seed:
            self.cli_by_seed[seed] = _FakeCli()
        return self.cli_by_seed[seed]

    async def open(
        self,
        *,
        context_id: ExecutionContextId,
        surface_kind: InputSurfaceKind,
        isolation_seed: str,
    ) -> _FakeContext:
        self.opened.append((context_id, surface_kind, isolation_seed))
        return _FakeContext(isolation_key=f"browser:{isolation_seed}")

    async def close(self, *, context_id: ExecutionContextId) -> None:
        self.closed.append(context_id.value)


# ---------------------------------------------------------------------------
# Helper: build adapter with fake factory
# ---------------------------------------------------------------------------


def _approved_for(_tenant_id: UUID) -> frozenset[str]:
    return frozenset({"approved.example.com"})


def _no_approved(_tenant_id: UUID) -> frozenset[str]:
    """Sin allowlist configurado → modo ABIERTO (writes permitidos, HITL es el gate)."""
    return frozenset()


def _restricted_other(_tenant_id: UUID) -> frozenset[str]:
    """Allowlist opt-in configurado que NO incluye el host actual del fake CLI."""
    return frozenset({"only-known.example.com"})


def _make_adapter(
    *,
    factory: _FakeFactory | None = None,
    approved_sites=_approved_for,
) -> tuple[BrowserSurfaceAdapter, _FakeFactory, BrowserSessionRegistry]:
    factory = factory or _FakeFactory()
    session_registry = BrowserSessionRegistry()

    adapter = BrowserSurfaceAdapter(
        factory=factory,
        registry=session_registry,
        approved_sites=approved_sites,
    )
    return adapter, factory, session_registry


def _action(
    *,
    op: str,
    work_item_id: UUID | None = None,
    tenant_id: UUID | None = _TENANT,
    url: str = "https://approved.example.com/",
    ref: str = "@e1",
    text: str = "hello",
) -> CapturedAction:
    payload: dict[str, Any] = {"op": op, "url": url, "ref": ref, "text": text}
    return CapturedAction(
        surface_kind=SurfaceKind.BROWSER,
        intent_desc=f"test op={op}",
        payload=payload,
        tenant_id=tenant_id,
        work_item_id=work_item_id,
    )


# ---------------------------------------------------------------------------
# Override _open_new_session to inject a fake CLI
# ---------------------------------------------------------------------------


def _patch_open_new_session(
    adapter: BrowserSurfaceAdapter,
    factory: _FakeFactory,
) -> None:
    """Replace _open_new_session so it returns a session with our _FakeCli."""

    async def _fake_open(*, work_item_id: UUID, tenant_id: UUID | None) -> BrowserTaskSession:
        from hermes.execution.domain.ports import InputSurfaceKind as ISK  # noqa: PLC0415

        seed = str(work_item_id)
        cli_fake = factory._cli_for(seed)
        context_id_value = UUID(int=work_item_id.int ^ 0xABCD)
        # Register open in factory for assertion
        from hermes.execution.domain.ports import (  # noqa: PLC0415
            ExecutionContextId,
            InputOwnerKind,
        )
        ctx_id = ExecutionContextId(value=context_id_value, owner_kind=InputOwnerKind.AGENT_TASK)
        factory.opened.append((ctx_id, ISK.BROWSER, seed))

        return BrowserTaskSession(
            context_id=context_id_value,
            cli=cli_fake,  # type: ignore[arg-type]
            site_id=None,
        )

    adapter._open_new_session = _fake_open  # type: ignore[method-assign]


# ===========================================================================
# Tests: session lifecycle
# ===========================================================================


class TestSessionReuse:
    """Same work_item_id → ONE session opened across multiple replays."""

    @pytest.mark.asyncio
    async def test_multiple_replays_reuse_one_session(self) -> None:
        adapter, factory, registry = _make_adapter()
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        n_replays = 3
        for _ in range(n_replays):
            outcome = await adapter.replay(
                _action(op="snapshot", work_item_id=wid),
            )
            assert outcome.status == ReplayStatus.EXECUTED_OK

        # Factory was asked to open exactly once.
        assert len(factory.opened) == 1

    @pytest.mark.asyncio
    async def test_distinct_work_item_ids_get_distinct_sessions(self) -> None:
        adapter, factory, registry = _make_adapter()
        _patch_open_new_session(adapter, factory)

        wid1, wid2 = uuid4(), uuid4()
        await adapter.replay(_action(op="snapshot", work_item_id=wid1))
        await adapter.replay(_action(op="snapshot", work_item_id=wid2))

        assert len(factory.opened) == 2
        seeds = [str(entry[2]) for entry in factory.opened]
        assert str(wid1) in seeds
        assert str(wid2) in seeds


# ===========================================================================
# Tests: close_task
# ===========================================================================


class TestCloseTask:
    """close_task closes factory exactly once and is idempotent."""

    @pytest.mark.asyncio
    async def test_close_task_calls_factory_close(self) -> None:
        adapter, factory, registry = _make_adapter()
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        await adapter.replay(_action(op="snapshot", work_item_id=wid))

        assert registry.get(wid) is not None
        await adapter.close_task(wid)

        assert registry.get(wid) is None
        assert len(factory.closed) == 1

    @pytest.mark.asyncio
    async def test_close_task_idempotent_on_unknown_id(self) -> None:
        adapter, factory, _ = _make_adapter()
        unknown = uuid4()
        # Must not raise.
        await adapter.close_task(unknown)
        assert factory.closed == []

    @pytest.mark.asyncio
    async def test_close_task_releases_even_when_verb_raised(self) -> None:
        """If a verb raises mid-task, close_task still releases correctly."""
        adapter, factory, registry = _make_adapter()

        wid = uuid4()
        # Seed a session that will raise on navigate.
        bad_cli = _FakeCli(raise_on_navigate=RuntimeError("network timeout"))
        context_id_value = uuid4()
        session = BrowserTaskSession(
            context_id=context_id_value,
            cli=bad_cli,  # type: ignore[arg-type]
            site_id=None,
        )
        registry.put(wid, session)
        factory.opened.append((None, None, str(wid)))  # type: ignore[arg-type]

        # The navigate verb raises — replay returns EXECUTED_FAILED.
        outcome = await adapter.replay(_action(op="navigate", work_item_id=wid))
        assert outcome.status == ReplayStatus.EXECUTED_FAILED

        # Session still in registry because navigate raised (not cleared by replay).
        # close_task must clean it up.
        await adapter.close_task(wid)
        assert registry.get(wid) is None


# ===========================================================================
# Tests: hybrid navigation policy
# ===========================================================================


class TestHybridPolicy:
    """Verify READ/WRITE policy enforcement."""

    @pytest.mark.asyncio
    async def test_write_on_non_approved_site_is_rejected(self) -> None:
        # Allowlist opt-in CONFIGURADO que no incluye el host → rechazado.
        adapter, factory, registry = _make_adapter(approved_sites=_restricted_other)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        outcome = await adapter.replay(
            _action(op="click", work_item_id=wid)
        )
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert "allowlist" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_write_empty_allowlist_is_denied(self) -> None:
        """Fix-5 / CTRL-5: sin allowlist configurado (vacío) → WRITE denegado (fail-closed).

        El comportamiento anterior era fail-open (WRITE permitido cuando la lista
        estaba vacía). Se invierte a fail-closed: el operador debe configurar
        approved_sites explícitamente para permitir WRITE en un host.
        El broker upstream ya forzó HITL para click/type_ (HIGH en registry);
        este gate añade una segunda comprobación de site en el adapter.
        """
        adapter, factory, registry = _make_adapter(approved_sites=_no_approved)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        outcome = await adapter.replay(
            _action(op="click", ref="@e1", work_item_id=wid)
        )
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY, (
            "Fix-5 regression: WRITE con allowlist vacío debe ser REJECTED_BY_POLICY. "
            "El adapter debe ser fail-closed para WRITE sin approved_sites."
        )

    @pytest.mark.asyncio
    async def test_write_on_approved_site_proceeds(self) -> None:
        adapter, factory, registry = _make_adapter(approved_sites=_approved_for)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        # The fake CLI current_url is https://approved.example.com/page
        outcome = await adapter.replay(
            _action(op="click", ref="@e1", work_item_id=wid)
        )
        assert outcome.status == ReplayStatus.EXECUTED_OK

    @pytest.mark.asyncio
    async def test_navigate_on_open_web_allowed(self) -> None:
        """navigate (READ) is allowed to any URL — no allowlist check."""
        adapter, factory, registry = _make_adapter(approved_sites=_no_approved)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        outcome = await adapter.replay(
            _action(op="navigate", url="https://evil.example.com/page", work_item_id=wid)
        )
        assert outcome.status == ReplayStatus.EXECUTED_OK

    @pytest.mark.asyncio
    async def test_navigate_rejects_non_http_scheme(self) -> None:
        """F-06: navigate a file:/chrome:/javascript: → rejected_by_policy."""
        adapter, factory, registry = _make_adapter(approved_sites=_no_approved)
        _patch_open_new_session(adapter, factory)

        for bad in (
            "file:///etc/passwd",
            "chrome://settings",
            "javascript:alert(1)",
            "data:text/html,<x>",
        ):
            wid = uuid4()
            outcome = await adapter.replay(
                _action(op="navigate", url=bad, work_item_id=wid)
            )
            assert outcome.status == ReplayStatus.REJECTED_BY_POLICY, bad

    @pytest.mark.asyncio
    async def test_snapshot_on_open_web_allowed(self) -> None:
        adapter, factory, registry = _make_adapter(approved_sites=_no_approved)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        outcome = await adapter.replay(_action(op="snapshot", work_item_id=wid))
        assert outcome.status == ReplayStatus.EXECUTED_OK

    @pytest.mark.asyncio
    async def test_read_url_allowed(self) -> None:
        adapter, factory, registry = _make_adapter(approved_sites=_no_approved)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        outcome = await adapter.replay(_action(op="read_url", work_item_id=wid))
        assert outcome.status == ReplayStatus.EXECUTED_OK

    @pytest.mark.asyncio
    async def test_type_on_non_approved_is_rejected(self) -> None:
        adapter, factory, registry = _make_adapter(approved_sites=_restricted_other)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        outcome = await adapter.replay(
            _action(op="type_", ref="@e1", text="hello", work_item_id=wid)
        )
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_write_rejected_does_not_mutate_session(self) -> None:
        """A rejected WRITE must not call cli.click or cli.type_."""
        adapter, factory, registry = _make_adapter(approved_sites=_restricted_other)
        _patch_open_new_session(adapter, factory)

        wid = uuid4()
        await adapter.replay(_action(op="click", ref="@e1", work_item_id=wid))
        session = registry.get(wid)
        assert session is not None
        cli = session.cli
        assert isinstance(cli, _FakeCli)
        assert cli.click_calls == []


# ===========================================================================
# Tests: error handling
# ===========================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_surface_mismatch_rejected(self) -> None:
        adapter, _, _ = _make_adapter()
        wrong = CapturedAction(
            surface_kind=SurfaceKind.TERMINAL,
            intent_desc="wrong surface",
            payload={"op": "snapshot"},
            work_item_id=uuid4(),
        )
        outcome = await adapter.replay(wrong)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert "mismatch" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_unknown_op_rejected(self) -> None:
        adapter, _, _ = _make_adapter()
        action = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="unknown op",
            payload={"op": "inject_script"},
            work_item_id=uuid4(),
        )
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert "inject_script" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_missing_work_item_id_rejected(self) -> None:
        adapter, _, _ = _make_adapter()
        action = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="no work_item_id",
            payload={"op": "snapshot"},
            work_item_id=None,
        )
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert "work_item_id" in (outcome.error or "")


# ===========================================================================
# Tests: INV-BROWSER-ADMISSION — PENDING_APPROVAL before adapter is called
# ===========================================================================


class TestBrokerGateBeforeAdapter:
    """A HIGH-risk binding with no HITL token must never reach adapter.replay.

    We drive through the real CapabilityBroker to assert that factory.open
    is NOT called when the broker returns PENDING_APPROVAL.
    """

    @pytest.mark.asyncio
    async def test_high_risk_no_token_never_opens_factory(self) -> None:
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner  # noqa: PLC0415
        from hermes.agents_os.infrastructure.sqlite_audit_repository import (  # noqa: PLC0415
            SqliteAuditRepository,
        )
        from hermes.capabilities.application.capability_broker import CapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.application.intent_log import IntentLog  # noqa: PLC0415
        from hermes.capabilities.domain.ports import (  # noqa: PLC0415
            CapabilityBinding,
            ConsentContext,
            ExecutionStatus,
            RiskLevel,
        )
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (  # noqa: PLC0415
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_registry import (  # noqa: PLC0415
            FakeCapabilityRegistry,
        )
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor  # noqa: PLC0415
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415

        signing_key = os.urandom(32)
        tmp = tempfile.mkdtemp()
        audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")
        signer = AuditHashChainSigner(signing_key=signing_key)
        intent_log = IntentLog()

        # Recording factory — must NOT be called.
        factory = _FakeFactory()
        session_registry = BrowserSessionRegistry()
        adapter = BrowserSurfaceAdapter(
            factory=factory,
            registry=session_registry,
            approved_sites=_approved_for,
        )

        registry = FakeCapabilityRegistry()
        registry.register(CapabilityBinding(
            tool_name="browser_navigate",
            surface_kind=SurfaceKind.BROWSER,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
        ))

        gate = FakeApprovalGate()
        dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.BROWSER: adapter})

        class _AlwaysActiveConsent:
            def assert_active(self, *, human_operator_id, capability):
                return object()
            def use(self, *, human_operator_id, capability):
                return object()

        broker = CapabilityBroker(
            registry=registry,
            consent_manager=_AlwaysActiveConsent(),
            approval_gate=gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            anchor=FakeExternalAnchor(),
        )

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="browser_navigate",
            tenant_id=_TENANT,
            entity_id="ent",
            entity_type="task",
            parameters={"op": "navigate", "url": "https://approved.example.com/"},
            justification="test",
        )
        ctx = ConsentContext(tenant_id=_TENANT, operator_id=uuid4())

        outcome = await broker.dispatch(
            proposal, ctx, hitl_approval_token=None, work_item_id=uuid4()
        )

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        # factory.open must NOT have been called (broker stopped before adapter).
        assert factory.opened == []


# ===========================================================================
# Tests: regression — stateless adapters unaffected by new work_item_id field
# ===========================================================================


class TestExistingAdaptersUnaffected:
    """Existing adapters must not be affected by the new optional work_item_id."""

    def test_captured_action_without_work_item_id_is_valid(self) -> None:
        """CapturedAction without work_item_id serializes correctly (Liskov)."""
        action = CapturedAction(
            surface_kind=SurfaceKind.FILESYSTEM,
            intent_desc="read file",
            payload={"op": "read_file", "path": "/tmp/x"},
        )
        assert action.work_item_id is None
        assert action.surface_kind == SurfaceKind.FILESYSTEM

    def test_captured_action_with_work_item_id_is_valid(self) -> None:
        wid = uuid4()
        action = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="navigate",
            payload={"op": "navigate", "url": "https://example.com"},
            work_item_id=wid,
        )
        assert action.work_item_id == wid

    @pytest.mark.asyncio
    async def test_filesystem_adapter_replay_unaffected(self) -> None:
        """FilesystemSurfaceAdapter still works with a CapturedAction that has work_item_id."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (  # noqa: PLC0415
            FilesystemSurfaceAdapter,
        )

        adapter = FilesystemSurfaceAdapter(allowed_prefixes=["/tmp"])
        action = CapturedAction(
            surface_kind=SurfaceKind.FILESYSTEM,
            intent_desc="read tmp",
            payload={"op": "read_file", "path": "/tmp/does_not_exist.txt"},
            work_item_id=uuid4(),
        )
        # Replay should return failed (file does not exist) but NOT raise.
        outcome = await adapter.replay(action)
        # REJECTED_BY_POLICY or EXECUTED_FAILED — just not an exception.
        assert outcome.status in (
            ReplayStatus.REJECTED_BY_POLICY,
            ReplayStatus.EXECUTED_FAILED,
        )

    @pytest.mark.asyncio
    async def test_api_call_adapter_surface_mismatch_unaffected(self) -> None:
        """ApiCallSurfaceAdapter rejects BROWSER surface — work_item_id field doesn't matter."""
        from hermes.agents_os.infrastructure.api_call_surface_adapter import (  # noqa: PLC0415
            ApiCallSurfaceAdapter,
        )

        adapter = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        action = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,  # wrong surface
            intent_desc="wrong",
            payload={"method": "GET", "url": "https://example.com/"},
            work_item_id=uuid4(),
        )
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY


# ===========================================================================
# Tests: _host_is_approved helper (pure function)
# ===========================================================================


class TestHostIsApproved:
    def test_exact_match(self) -> None:
        assert _host_is_approved("example.com", frozenset({"example.com"}))

    def test_subdomain_match(self) -> None:
        assert _host_is_approved("api.example.com", frozenset({"example.com"}))

    def test_no_match(self) -> None:
        assert not _host_is_approved("evil.com", frozenset({"example.com"}))

    def test_empty_approved_set(self) -> None:
        assert not _host_is_approved("example.com", frozenset())

    def test_empty_host(self) -> None:
        assert not _host_is_approved("", frozenset({"example.com"}))

    def test_case_insensitive(self) -> None:
        assert _host_is_approved("EXAMPLE.COM", frozenset({"example.com"}))


# ===========================================================================
# Tests: capture (passive)
# ===========================================================================


class TestCapture:
    @pytest.mark.asyncio
    async def test_capture_does_not_include_storage_state(self) -> None:
        adapter, _, _ = _make_adapter()
        action = await adapter.capture(
            intent_desc="login",
            params={"op": "navigate", "url": "https://example.com", "storage_state": "SECRET"},
            tenant_id=_TENANT,
            human_operator_id=uuid4(),
        )
        assert "storage_state" not in action.payload

    @pytest.mark.asyncio
    async def test_capture_returns_browser_surface_kind(self) -> None:
        adapter, _, _ = _make_adapter()
        action = await adapter.capture(
            intent_desc="nav",
            params={"op": "navigate", "url": "https://example.com"},
            tenant_id=_TENANT,
            human_operator_id=uuid4(),
        )
        assert action.surface_kind == SurfaceKind.BROWSER


# ===========================================================================
# Tests: serialize_for_signing (determinism)
# ===========================================================================


class TestSerializeForSigning:
    def test_deterministic(self) -> None:
        adapter, _, _ = _make_adapter()
        action = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="nav",
            payload={"op": "navigate", "url": "https://example.com"},
        )
        assert adapter.serialize_for_signing(action) == adapter.serialize_for_signing(action)

    def test_storage_state_absent_from_signing_bytes(self) -> None:
        adapter, _, _ = _make_adapter()
        action = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="nav",
            payload={"op": "navigate", "url": "https://example.com", "storage_state": "SECRET"},
        )
        signed = adapter.serialize_for_signing(action)
        assert b"SECRET" not in signed
        assert b"storage_state" not in signed
