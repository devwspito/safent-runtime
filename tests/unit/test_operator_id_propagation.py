"""Regression tests: spec 014 inc. 3 — operator_id propagation (CTRL-13 fix).

Verifies that when the daemon starts without HERMES_OPERATOR_ID (operator_id=None
in the daemon-level ConsentContext), native capability tool calls from the Nous
engine still reach broker.dispatch with a valid operator_id derived from
item.payload["enqueued_by"] (set server-side by ControlPlaneService — CTRL-P1-3).

Without the fix, broker.dispatch received ConsentContext(operator_id=None) and
CTRL-13 rejected every WRITE and READ with "operator_id ausente — fail-closed".

Security invariant: operator_id NEVER comes from LLM tool arguments or any
LLM-reachable parameter. Its only source is item.payload["enqueued_by"], which
ControlPlaneService sets from channel.sender_uid (POSIX-verified UID).

Covers:
  (A) _extract_enqueued_by_uuid: valid UUID string → UUID.
  (B) _extract_enqueued_by_uuid: absent/invalid → None (fail-safe).
  (C) _override_operator_id: overrides None with task operator_id.
  (D) _override_operator_id: preserves existing operator_id (daemon had one).
  (E) _inject_task_operator_id: injects UUID into DecisionContext.metadata.
  (F) _inject_task_operator_id: no-op when operator_id is None.
  (G) _resolve_per_cycle_consent: injects task operator_id when base is None.
  (H) _resolve_per_cycle_consent: preserves base when it already has operator_id.
  (I) _resolve_per_cycle_consent: returns base when no task_operator_id in metadata.
  (J) WRITE path: GovernedAIAgent dispatches with real operator_id (not None).
  (K) WRITE fail-closed: absent enqueued_by → operator_id=None → CTRL-13 rejects.
  (L) READ path: capability READ handler receives per-cycle operator_id via consent_ref.
  (M) consent_ref update: updating consent_ref[0] propagates to READ handler.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import (
    ConsentContext,
    ExecutionOutcome,
    ExecutionStatus,
)
from hermes.domain.decision_context import DecisionContext
from hermes.tasks.application.agent_loop_orchestrator import (
    _extract_enqueued_by_uuid,
    _inject_task_operator_id,
    _override_operator_id,
)
from hermes.tasks.domain.ports import WorkItem, WorkItemKind

pytestmark = pytest.mark.unit

_TENANT = UUID("aa000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("00000000-0000-0000-0000-0000000003e8")  # int=1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _work_item(enqueued_by: str | None = str(_OPERATOR)) -> WorkItem:
    payload: dict[str, Any] = {"instruction": "test"}
    if enqueued_by is not None:
        payload["enqueued_by"] = enqueued_by
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        payload=payload,
        kind=WorkItemKind.CHAT_MESSAGE,
    )


def _ctx_no_operator() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=None)


def _ctx_with_operator(uid: UUID = _OPERATOR) -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=uid)


def _decision_context(metadata: dict | None = None) -> DecisionContext:
    return DecisionContext(
        tenant_id=_TENANT,
        cycle_id=uuid4(),
        trigger="queue_drain:chat_message",
        subjects=(),
        constraints={},
        operator_instruction="do something",
        metadata=metadata or {},
    )


def _outcome(status: ExecutionStatus) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=status,
        result={},
    )


# ---------------------------------------------------------------------------
# (A) _extract_enqueued_by_uuid: valid UUID → UUID
# ---------------------------------------------------------------------------


class TestExtractEnqueuedByUuid:
    def test_valid_uuid_string_returns_uuid(self) -> None:
        item = _work_item(enqueued_by=str(_OPERATOR))
        result = _extract_enqueued_by_uuid(item)
        assert result == _OPERATOR

    def test_absent_enqueued_by_returns_none(self) -> None:
        item = _work_item(enqueued_by=None)
        result = _extract_enqueued_by_uuid(item)
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        item = _work_item(enqueued_by="")
        result = _extract_enqueued_by_uuid(item)
        assert result is None

    def test_invalid_uuid_returns_none(self) -> None:
        item = _work_item(enqueued_by="not-a-uuid")
        result = _extract_enqueued_by_uuid(item)
        assert result is None

    def test_different_valid_uid(self) -> None:
        uid = UUID(int=42)
        item = _work_item(enqueued_by=str(uid))
        assert _extract_enqueued_by_uuid(item) == uid


# ---------------------------------------------------------------------------
# (C/D) _override_operator_id
# ---------------------------------------------------------------------------


class TestOverrideOperatorId:
    def test_overrides_none_with_task_operator_id(self) -> None:
        base = _ctx_no_operator()
        result = _override_operator_id(base, _OPERATOR)
        assert result.operator_id == _OPERATOR
        assert result.tenant_id == _TENANT

    def test_preserves_existing_operator_id(self) -> None:
        existing = UUID(int=999)
        base = _ctx_with_operator(existing)
        result = _override_operator_id(base, _OPERATOR)
        # base already has an operator_id → preserve it, don't override
        assert result.operator_id == existing

    def test_none_task_operator_id_returns_base_unchanged(self) -> None:
        base = _ctx_no_operator()
        result = _override_operator_id(base, None)
        assert result is base

    def test_preserves_taint_flag(self) -> None:
        base = ConsentContext(
            tenant_id=_TENANT,
            operator_id=None,
            derived_from_untrusted_content=True,
        )
        result = _override_operator_id(base, _OPERATOR)
        assert result.derived_from_untrusted_content is True


# ---------------------------------------------------------------------------
# (E/F) _inject_task_operator_id
# ---------------------------------------------------------------------------


class TestInjectTaskOperatorId:
    def test_injects_uuid_into_metadata(self) -> None:
        ctx = _decision_context()
        result = _inject_task_operator_id(ctx, _OPERATOR)
        assert result.metadata.get("task_operator_id") == _OPERATOR

    def test_preserves_existing_metadata(self) -> None:
        ctx = _decision_context(metadata={"chunk_sink": "x"})
        result = _inject_task_operator_id(ctx, _OPERATOR)
        assert result.metadata.get("chunk_sink") == "x"
        assert result.metadata.get("task_operator_id") == _OPERATOR

    def test_no_op_when_operator_id_is_none(self) -> None:
        ctx = _decision_context()
        result = _inject_task_operator_id(ctx, None)
        assert result is ctx
        assert "task_operator_id" not in result.metadata


# ---------------------------------------------------------------------------
# (G/H/I) _resolve_per_cycle_consent
# ---------------------------------------------------------------------------


class TestResolvePerCycleConsent:
    def test_injects_task_operator_id_when_base_has_none(self) -> None:
        from hermes.runtime.nous_engine import _resolve_per_cycle_consent

        base = _ctx_no_operator()
        ctx = _decision_context(metadata={"task_operator_id": _OPERATOR})
        result = _resolve_per_cycle_consent(base, ctx)

        assert result is not None
        assert result.operator_id == _OPERATOR
        assert result.tenant_id == _TENANT

    def test_preserves_base_when_it_already_has_operator_id(self) -> None:
        from hermes.runtime.nous_engine import _resolve_per_cycle_consent

        existing = UUID(int=42)
        base = _ctx_with_operator(existing)
        ctx = _decision_context(metadata={"task_operator_id": _OPERATOR})
        result = _resolve_per_cycle_consent(base, ctx)

        # base had a valid operator_id → keep it
        assert result is not None
        assert result.operator_id == existing

    def test_returns_base_when_no_task_operator_id_in_metadata(self) -> None:
        from hermes.runtime.nous_engine import _resolve_per_cycle_consent

        base = _ctx_no_operator()
        ctx = _decision_context()  # no task_operator_id
        result = _resolve_per_cycle_consent(base, ctx)

        assert result is base

    def test_returns_none_when_base_is_none(self) -> None:
        from hermes.runtime.nous_engine import _resolve_per_cycle_consent

        ctx = _decision_context(metadata={"task_operator_id": _OPERATOR})
        result = _resolve_per_cycle_consent(None, ctx)
        assert result is None


# ---------------------------------------------------------------------------
# (J) WRITE path: GovernedAIAgent dispatches with real operator_id (not None)
# ---------------------------------------------------------------------------


class TestWritePathOperatorIdPropagation:
    def test_dispatch_write_receives_per_cycle_operator_id(self) -> None:
        """A WRITE tool call from GovernedAIAgent dispatches to broker with
        the per-cycle operator_id from enqueued_by, not the daemon-level None."""
        from hermes.runtime.nous_engine import GovernedAIAgent

        dispatched_consent_contexts: list[ConsentContext] = []

        def fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatched_consent_contexts.append(consent_context)
            return _outcome(ExecutionStatus.PENDING_APPROVAL)

        fake_inner = MagicMock()
        loop = asyncio.new_event_loop()

        # Daemon started without HERMES_OPERATOR_ID → consent_context.operator_id=None
        # But the per-cycle consent (resolved from enqueued_by) has the real operator
        per_cycle_consent = _ctx_with_operator(_OPERATOR)

        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import, \
             patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_bridge):
            mock_ai_cls = MagicMock(return_value=fake_inner)
            mock_import.return_value = mock_ai_cls

            agent = GovernedAIAgent(
                broker=MagicMock(),
                consent_context=per_cycle_consent,  # per-cycle override applied
                engine_loop=loop,
                tenant_id=_TENANT,
            )

            # Call dispatch INSIDE the patch context so fake_bridge is active
            agent._dispatch_write_proposal(
                function_name="write_file",
                function_args={"path": "/tmp/x.txt", "content": "hello"},
                effective_task_id="task-123",
                tool_call_id=None,
            )

        assert len(dispatched_consent_contexts) == 1
        ctx = dispatched_consent_contexts[0]
        assert ctx.operator_id == _OPERATOR, (
            f"Expected operator_id={_OPERATOR}, got {ctx.operator_id}. "
            "CTRL-13 would reject this dispatch."
        )

        loop.close()


# ---------------------------------------------------------------------------
# (K) fail-closed: absent enqueued_by → broker receives None → CTRL-13 blocks
# ---------------------------------------------------------------------------


class TestFailClosedAbsentEnqueuedBy:
    def test_absent_enqueued_by_keeps_none_operator_id(self) -> None:
        """Without enqueued_by, the daemon-level None is preserved.
        The broker's CTRL-13 gate must reject it — fail-closed is CORRECT here.
        """
        item = _work_item(enqueued_by=None)
        task_operator_id = _extract_enqueued_by_uuid(item)
        assert task_operator_id is None

        base = _ctx_no_operator()
        result = _override_operator_id(base, task_operator_id)
        assert result.operator_id is None, (
            "When enqueued_by is absent, operator_id must remain None. "
            "The broker's CTRL-13 gate is responsible for rejecting the dispatch."
        )


# ---------------------------------------------------------------------------
# (L/M) READ path: consent_ref updates propagate to READ handler closures
# ---------------------------------------------------------------------------


class TestConsentRefPropagationToReadHandlers:
    @pytest.mark.asyncio
    async def test_read_handler_uses_updated_consent_ref(self) -> None:
        """Updating consent_ref[0] propagates to READ handler without rebuild."""
        from hermes.runtime.capability_tool_specs import build_capability_tool_specs

        dispatched_contexts: list[ConsentContext] = []

        broker = MagicMock()

        async def _capturing_dispatch(proposal, consent_ctx, **kwargs):
            dispatched_contexts.append(consent_ctx)
            return _outcome(ExecutionStatus.EXECUTED)

        broker.dispatch = _capturing_dispatch

        # Build specs with daemon-level consent (operator_id=None)
        daemon_consent = _ctx_no_operator()
        specs, consent_ref = build_capability_tool_specs(
            broker=broker,
            consent_context=daemon_consent,
        )

        lo_open = next((s for s in specs if s.name == "lo_open_document"), None)
        assert lo_open is not None and lo_open.handler is not None

        # Simulate what the engine does per-cycle: update consent_ref[0]
        per_cycle_consent = _ctx_with_operator(_OPERATOR)
        consent_ref[0] = per_cycle_consent

        await lo_open.handler({"document_path": "/tmp/test.odt"})

        assert len(dispatched_contexts) == 1
        assert dispatched_contexts[0].operator_id == _OPERATOR, (
            f"READ handler dispatched with operator_id={dispatched_contexts[0].operator_id}. "
            f"Expected {_OPERATOR} (from per-cycle consent_ref update). "
            "CTRL-13 would have rejected the daemon-level None."
        )

    @pytest.mark.asyncio
    async def test_read_handler_uses_daemon_consent_before_any_cycle(self) -> None:
        """Before the first cycle update, READ handler uses the daemon-level consent."""
        from hermes.runtime.capability_tool_specs import build_capability_tool_specs

        dispatched_contexts: list[ConsentContext] = []
        broker = MagicMock()

        async def _dispatch(proposal, consent_ctx, **kwargs):
            dispatched_contexts.append(consent_ctx)
            return _outcome(ExecutionStatus.EXECUTED)

        broker.dispatch = _dispatch

        daemon_consent = _ctx_with_operator(_OPERATOR)
        specs, _ref = build_capability_tool_specs(
            broker=broker,
            consent_context=daemon_consent,
        )

        lo_open = next(s for s in specs if s.name == "lo_open_document")
        await lo_open.handler({"document_path": "/tmp/test.odt"})

        assert dispatched_contexts[0].operator_id == _OPERATOR
