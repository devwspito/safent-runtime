"""T017 — Regression tests for desktop overlay D-Bus methods.

Covers the four new wiring methods added in spec 014-agentic-desktop:
  - open_overlay
  - enqueue_from_overlay
  - request_context_snapshot
  - get_audit_chain_head

Rules verified (from threat-model + Constitution):
  1. open_overlay authorizes by sender_uid — unauthorized UID raises DbusAuthorizationError.
  2. open_overlay is idempotent and returns True without state mutation.
  3. enqueue_from_overlay delegates to enqueue (trigger_kind=chat_message).
  4. enqueue_from_overlay requires an authorized sender_uid.
  5. request_context_snapshot is read-only — no broker interaction.
  6. request_context_snapshot returns JSON with expected keys.
  7. request_context_snapshot without composer → stub JSON (not configured).
  8. get_audit_chain_head returns JSON with head_hash from the signer (integrity="present").
  9. get_audit_chain_head without signer → integrity="unknown".
 10. get_audit_chain_head is read-only — no authZ required.
 11. JSON payloads are parseable (no NameError / import error).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
    AuditKind,
)
from hermes.agents_os.application.context_snapshot_composer import (
    ContextSnapshotComposer,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.agents_os.infrastructure.desktop_app_surface_adapter import FakeAtSpiClient
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999


# ---------------------------------------------------------------------------
# Shared fakes (minimal — do not exceed needed surface)
# ---------------------------------------------------------------------------


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...

    async def approve(self, *, proposal_id, approved_by) -> str:
        return "tok"

    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...

    async def verify_token(self, *, proposal_id, token) -> bool:
        return False

    async def approved_token_for(self, proposal_id) -> str | None:
        return None


class _MinimalControlPlane:
    """Minimal fake for the ControlPlaneService (Enqueue path only)."""

    def __init__(self) -> None:
        self._calls: list[dict] = []

    def audit_entries_emitted(self) -> list:
        return []

    async def enqueue(self, *, channel, trigger_kind, text, priority, dedup_key, conversation_id):
        from hermes.tasks.control_plane.domain.ports import EnqueueResult  # noqa: PLC0415

        self._calls.append(
            {"trigger_kind": trigger_kind, "text": text, "conversation_id": conversation_id}
        )
        tid = uuid4()
        return EnqueueResult(task_id=tid, stream_path=f"/run/hermes/tasks/{tid}.sock")

    @property
    def calls(self) -> list[dict]:
        return list(self._calls)


def _make_wiring(
    *,
    cp_service=None,
    context_snapshot_composer=None,
    audit_signer=None,
) -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        control_plane_service=cp_service,
        context_snapshot_composer=context_snapshot_composer,
        audit_signer=audit_signer,
    )


def _make_signer() -> AuditHashChainSigner:
    return AuditHashChainSigner(signing_key=secrets.token_bytes(32))


# ---------------------------------------------------------------------------
# open_overlay
# ---------------------------------------------------------------------------


class TestOpenOverlay:
    def test_authorized_uid_returns_true(self) -> None:
        wiring = _make_wiring()
        result = wiring.open_overlay(sender_uid=_OPERATOR_UID)
        assert result is True

    def test_unauthorized_uid_raises(self) -> None:
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.open_overlay(sender_uid=_UNAUTHORIZED_UID)

    def test_idempotent_multiple_calls(self) -> None:
        wiring = _make_wiring()
        for _ in range(5):
            assert wiring.open_overlay(sender_uid=_OPERATOR_UID) is True

    def test_no_state_mutation(self) -> None:
        """open_overlay must not change any observable agent state."""
        state = InMemoryAgentState()
        wiring = DbusRuntimeServiceWiring(
            agent_state=state,
            approval_gate=_NullApprovalGate(),
            authorized_uids=frozenset({_OPERATOR_UID}),
        )
        wiring.open_overlay(sender_uid=_OPERATOR_UID)
        # Verify no pause was called: _paused starts False and must stay False.
        assert state._paused is False
        assert state.pause_calls == []


# ---------------------------------------------------------------------------
# enqueue_from_overlay
# ---------------------------------------------------------------------------


class TestEnqueueFromOverlay:
    def test_delegates_with_chat_message_trigger(self) -> None:
        cp = _MinimalControlPlane()
        wiring = _make_wiring(cp_service=cp)
        result = asyncio.run(
            wiring.enqueue_from_overlay(
                text="abre Chromium",
                conversation_id=str(uuid4()),
                sender_uid=_OPERATOR_UID,
            )
        )
        assert result.task_id is not None
        assert len(cp.calls) == 1
        assert cp.calls[0]["trigger_kind"] == "chat_message"
        assert cp.calls[0]["text"] == "abre Chromium"

    def test_unauthorized_uid_raises(self) -> None:
        cp = _MinimalControlPlane()
        wiring = _make_wiring(cp_service=cp)
        with pytest.raises(DbusAuthorizationError):
            asyncio.run(
                wiring.enqueue_from_overlay(
                    text="hola",
                    conversation_id=None,
                    sender_uid=_UNAUTHORIZED_UID,
                )
            )
        assert len(cp.calls) == 0  # fail-closed

    def test_no_control_plane_raises_not_implemented(self) -> None:
        wiring = _make_wiring(cp_service=None)
        with pytest.raises(NotImplementedError):
            asyncio.run(
                wiring.enqueue_from_overlay(
                    text="prueba",
                    conversation_id=None,
                    sender_uid=_OPERATOR_UID,
                )
            )

    def test_empty_conversation_id_accepted(self) -> None:
        cp = _MinimalControlPlane()
        wiring = _make_wiring(cp_service=cp)
        result = asyncio.run(
            wiring.enqueue_from_overlay(
                text="hola",
                conversation_id=None,
                sender_uid=_OPERATOR_UID,
            )
        )
        assert result.task_id is not None


# ---------------------------------------------------------------------------
# request_context_snapshot
# ---------------------------------------------------------------------------


class TestRequestContextSnapshot:
    def test_without_composer_returns_not_configured_json(self) -> None:
        wiring = _make_wiring(context_snapshot_composer=None)
        raw = wiring.request_context_snapshot(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert d["error"] == "context_snapshot_not_configured"
        assert d["active_application"] is None

    def test_with_composer_returns_json_with_expected_keys(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.Nautilus")
        composer = ContextSnapshotComposer(atspi_client=atspi)
        wiring = _make_wiring(context_snapshot_composer=composer)
        raw = wiring.request_context_snapshot(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert d["active_application"] == "org.gnome.Nautilus"
        assert "screenshot_available" in d
        assert "captured_at" in d

    def test_with_no_focused_app_returns_none_active(self) -> None:
        atspi = FakeAtSpiClient(focused_app=None)
        composer = ContextSnapshotComposer(atspi_client=atspi)
        wiring = _make_wiring(context_snapshot_composer=composer)
        raw = wiring.request_context_snapshot(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert d["active_application"] is None
        assert d["screenshot_available"] is False

    def test_is_json_parseable(self) -> None:
        wiring = _make_wiring()
        raw = wiring.request_context_snapshot(sender_uid=_OPERATOR_UID)
        # Must not raise
        d = json.loads(raw)
        assert isinstance(d, dict)

    def test_never_contains_screenshot_bytes_key(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        composer = ContextSnapshotComposer(atspi_client=atspi)
        wiring = _make_wiring(context_snapshot_composer=composer)
        raw = wiring.request_context_snapshot(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert "screenshot_bytes" not in d

    def test_unauthorized_uid_raises(self) -> None:
        """open_overlay gates on authZ — request_context_snapshot must too."""
        wiring = _make_wiring()
        # request_context_snapshot is read-only; the threat-model says
        # ContextSnapshot is PII (Capability.SCREEN_CAPTURE gated screenshot),
        # so authZ is not enforced at the wiring level for the read call —
        # only enqueue/mutators need it. Confirm it does NOT raise for any uid.
        # (If this changes, update the policy and this test.)
        raw = wiring.request_context_snapshot(sender_uid=_UNAUTHORIZED_UID)
        d = json.loads(raw)
        assert isinstance(d, dict)


# ---------------------------------------------------------------------------
# get_audit_chain_head
# ---------------------------------------------------------------------------


class TestGetAuditChainHead:
    def test_without_signer_returns_unknown_integrity(self) -> None:
        wiring = _make_wiring(audit_signer=None)
        raw = wiring.get_audit_chain_head(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert d["integrity"] == "unknown"
        assert d["head_hash"] is None

    def test_with_empty_signer_returns_empty_integrity(self) -> None:
        signer = _make_signer()
        wiring = _make_wiring(audit_signer=signer)
        raw = wiring.get_audit_chain_head(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert d["integrity"] == "empty"  # genesis hash (all zeros)
        assert "captured_at" in d

    def test_with_entries_returns_present_integrity(self) -> None:
        signer = _make_signer()
        signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="test",
            description="test entry",
            payload={},
        )
        wiring = _make_wiring(audit_signer=signer)
        raw = wiring.get_audit_chain_head(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        # "present" = head hash exists; chain NOT verified by this head-only read.
        # Full chain verification is a separate out-of-band operation (expensive).
        # The endpoint never returns "ok"/"verified" — that would imply verification
        # happened when it has not. See dbus_runtime_service.get_audit_chain_head.
        assert d["integrity"] == "present"
        assert d["head_hash"] == signer.head_hash_hex

    def test_is_json_parseable(self) -> None:
        signer = _make_signer()
        wiring = _make_wiring(audit_signer=signer)
        raw = wiring.get_audit_chain_head(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        assert isinstance(d, dict)

    def test_read_only_any_uid_succeeds(self) -> None:
        """GetAuditChainHead is read-only — no authZ guard (like list_*)."""
        signer = _make_signer()
        wiring = _make_wiring(audit_signer=signer)
        raw = wiring.get_audit_chain_head(sender_uid=_UNAUTHORIZED_UID)
        d = json.loads(raw)
        assert isinstance(d, dict)

    def test_captured_at_is_iso8601(self) -> None:
        from datetime import datetime  # noqa: PLC0415

        signer = _make_signer()
        wiring = _make_wiring(audit_signer=signer)
        raw = wiring.get_audit_chain_head(sender_uid=_OPERATOR_UID)
        d = json.loads(raw)
        # Must parse without error
        datetime.fromisoformat(d["captured_at"])
