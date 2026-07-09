"""Regression test: LIVE vs POST approval differentiation (2026-06-25).

Bug: approvals_api always returned {"ok": True} regardless of whether
signal_native_danger_approval found a waiting thread (LIVE) or not (POST).
Frontend showed "Acción permitida. El agente continúa." on both paths —
owner approved a timed-out card 5× seeing false success each time.

Root cause:
  approve_action returns HitlApprovalResult(thread_resumed: bool).
  D-Bus adapter serialises it as JSON {"token": ..., "live": bool}.
  DbusControlPlaneAdapter.approve() returns that raw JSON string.
  approvals_api.resolve_approval() discarded the return value, always
  returning {"ok": True} — live/post distinction was silently dropped.

Fix:
  approvals_api.resolve_approval() parses the JSON string and includes
  "live" in the HTTP response. Frontend differentiates:
    live=true  → "Acción aprobada y ejecutada." (it really ran).
    live=false → "La solicitud ya había caducado — acción no ejecutada."
                 sets card to 'expired'; owner knows to re-ask the agent.

Tests:
  A. signal_native_danger_approval with a matching proposal_id → True (LIVE).
  B. signal_native_danger_approval with no waiting slot → False (POST).
  C. approvals_api response includes live=True when D-Bus says {"live": true}.
  D. approvals_api response includes live=False when D-Bus says {"live": false}.
  E. approvals_api defaults live=True when D-Bus returns non-JSON (fallback path).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


# ────────────────────────────────────────────────────────────────────────────
# A/B — signal_native_danger_approval: matching vs non-matching proposal_id
# ────────────────────────────────────────────────────────────────────────────

class TestSignalLiveVsPost:
    """signal_native_danger_approval returns True (LIVE) xor False (POST)."""

    def _inject_slot(self, proposal_id: str) -> dict:
        from hermes.runtime.security_hook import _register_pending_event
        event = threading.Event()
        slot: dict = {"event": event, "choice": None}
        _register_pending_event(proposal_id, slot)
        return slot

    def _remove_slot(self, proposal_id: str) -> None:
        from hermes.runtime.security_hook import _pending_events, _pending_events_lock
        with _pending_events_lock:
            _pending_events.pop(proposal_id, None)

    def test_A_matching_proposal_id_fires_event_returns_true(self) -> None:
        """LIVE: signal with the exact proposal_id the hook is waiting on → True."""
        from hermes.runtime.security_hook import signal_native_danger_approval

        pid = str(uuid4())
        slot = self._inject_slot(pid)
        try:
            result = signal_native_danger_approval(pid, "approved")
            assert result is True, (
                "Expected True (LIVE thread found) but got False — "
                "approve_action would not wake the blocked conversation thread"
            )
            assert slot["event"].is_set(), "Event was not set — blocked thread stays blocked"
            assert slot["choice"] == "approved"
        finally:
            self._remove_slot(pid)

    def test_B_non_matching_proposal_id_buffers(self) -> None:
        """Fast-approval race: a proposal_id with no waiting slot BUFFERS the decision
        (TTL-bounded) and returns True so the imminently-registering waiter consumes
        it. Returning False here was the false "La acción caducó" toast on a valid
        ~2s approval; the real-flow safety is gate.approve (only a still-pending
        proposal reaches signal) + the 2s TTL that drops an unclaimed buffer."""
        from hermes.runtime.security_hook import (
            _pending_events_lock,
            _presignals,
            signal_native_danger_approval,
        )

        pid = str(uuid4())
        try:
            assert signal_native_danger_approval(pid, "approved") is True
            with _pending_events_lock:
                assert _presignals.get(pid, (None,))[0] == "approved"
        finally:
            with _pending_events_lock:
                _presignals.pop(pid, None)

    def test_B2_buffers_after_slot_cleaned_up(self) -> None:
        """After the slot is cleaned up, signal buffers (TTL-bounded) and returns True.
        A genuinely timed-out proposal never reaches signal in the real flow
        (gate.approve raises first); an unclaimed buffer expires within the 2s TTL and
        so cannot resume a dead turn."""
        from hermes.runtime.security_hook import (
            _pending_events_lock,
            _presignals,
            signal_native_danger_approval,
        )

        pid = str(uuid4())
        self._inject_slot(pid)
        self._remove_slot(pid)  # simulate timeout cleanup
        try:
            assert signal_native_danger_approval(pid, "approved") is True
        finally:
            with _pending_events_lock:
                _presignals.pop(pid, None)


# ────────────────────────────────────────────────────────────────────────────
# C/D/E — approvals_api: live field propagated in HTTP response
# ────────────────────────────────────────────────────────────────────────────

def _make_fake_control_plane(dbus_json_response: str | None):
    """Build a fake ControlPlane whose .approve() returns dbus_json_response."""

    class FakeCP:
        async def approve(self, *, channel, proposal_id, mfa_factors=None):
            return dbus_json_response

    return FakeCP()


@pytest.mark.asyncio
async def test_C_approvals_api_returns_live_true_when_dbus_says_live() -> None:
    """C — live=true from D-Bus propagates to the HTTP response body."""
    from hermes.shell_server.cowork.approvals_api import create_approvals_router
    from hermes.shell_server.security.mfa import MfaStore
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    live_json = json.dumps({"token": "tok-abc", "live": True})
    cp = _make_fake_control_plane(live_json)

    app = FastAPI()
    app.state.control_plane = cp
    app.include_router(create_approvals_router(mfa=MfaStore()))

    client = TestClient(app, raise_server_exceptions=True)
    pid = str(uuid4())

    resp = client.post(
        f"/api/v1/approvals/{pid}",
        json={"decision": "once", "totp": None},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("live") is True, (
        f"Expected live=True in response (LIVE path), got: {body}"
    )


@pytest.mark.asyncio
async def test_D_approvals_api_returns_live_false_when_dbus_says_post() -> None:
    """D — live=false from D-Bus propagates to the HTTP response body (POST path)."""
    from hermes.shell_server.cowork.approvals_api import create_approvals_router
    from hermes.shell_server.security.mfa import MfaStore
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    post_json = json.dumps({"token": "tok-xyz", "live": False})
    cp = _make_fake_control_plane(post_json)

    app = FastAPI()
    app.state.control_plane = cp
    app.include_router(create_approvals_router(mfa=MfaStore()))

    client = TestClient(app, raise_server_exceptions=True)
    pid = str(uuid4())

    resp = client.post(
        f"/api/v1/approvals/{pid}",
        json={"decision": "once", "totp": None},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("live") is False, (
        f"Expected live=False in response (POST path), got: {body}\n"
        "Before fix: live was absent — frontend always showed success toast "
        "even when the turn had already ended and the action did NOT execute."
    )


@pytest.mark.asyncio
async def test_E_approvals_api_defaults_live_true_for_non_json_response() -> None:
    """E — if D-Bus returns a non-JSON string, live defaults to True (safe fallback)."""
    from hermes.shell_server.cowork.approvals_api import create_approvals_router
    from hermes.shell_server.security.mfa import MfaStore
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    cp = _make_fake_control_plane("plain-token-string-not-json")

    app = FastAPI()
    app.state.control_plane = cp
    app.include_router(create_approvals_router(mfa=MfaStore()))

    client = TestClient(app, raise_server_exceptions=True)
    pid = str(uuid4())

    resp = client.post(
        f"/api/v1/approvals/{pid}",
        json={"decision": "once", "totp": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("live") is True, (
        f"Non-JSON D-Bus response should default live=True, got: {body}"
    )


@pytest.mark.asyncio
async def test_E2_approvals_api_defaults_live_true_for_none_response() -> None:
    """E2 — if D-Bus returns None (non-D-Bus adapter), live defaults to True."""
    from hermes.shell_server.cowork.approvals_api import create_approvals_router
    from hermes.shell_server.security.mfa import MfaStore
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    cp = _make_fake_control_plane(None)

    app = FastAPI()
    app.state.control_plane = cp
    app.include_router(create_approvals_router(mfa=MfaStore()))

    client = TestClient(app, raise_server_exceptions=True)
    pid = str(uuid4())

    resp = client.post(
        f"/api/v1/approvals/{pid}",
        json={"decision": "once", "totp": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("live") is True, (
        f"None D-Bus response should default live=True, got: {body}"
    )
