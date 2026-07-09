"""Per-SESSION browser consent gate (owner decision 2026-07-09).

Root cause fixed here: browser_* (WRITE) is DELICATE-not-MOST_DELICATE and enabled
by default in the Equilibrado preset, so the owner-preapproval short-circuit in
_pre_tool_call_hook cleared its MFA requirement → _resolve_native_danger_approval
was NEVER reached → no approval card ever appeared and the browser auto-executed.

The fix routes browser_* through a dedicated per-SESSION gate BEFORE Step 1.6:
  - the FIRST browser_* in a conversation surfaces ONE card (per-conversation
    proposal key, so concurrent/subsequent browser actions share the one decision);
  - once approved, the whole conversation is marked → later browser_* ALLOW, no card;
  - always-ask (decoupled from mfa_on_dangers);
  - the kernel floor (terminal/code/service) is untouched.

These tests lock:
1. _is_browser_session_tool classification (browser_* yes; web_search / others no).
2. Per-conversation approval state: mark / is-approved / bounded eviction.
3. _resolve_browser_session_consent: approve marks, deny does NOT, already-approved
   short-circuits (no resolver call), and the resolver gets the per-conversation key.
4. session_key derivation: SAME conversation → SAME proposal_id regardless of args
   (one card per session); no session_key → the per-action digest (unchanged).
5. browser liveness probe: real-URL detection + fail-soft.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_URL, uuid5

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Tool predicate
# ---------------------------------------------------------------------------

class TestBrowserSessionToolPredicate:
    def test_browser_tools_match(self) -> None:
        from hermes.runtime.security_hook import _is_browser_session_tool

        for t in (
            "browser_navigate", "browser_click", "browser_type", "browser_scroll",
            "browser_press", "browser_snapshot", "browser_back", "browser_get_images",
            "browser_console", "browser_vision", "browser_cdp", "browser_dialog",
        ):
            assert _is_browser_session_tool(t) is True, t

    def test_non_browser_tools_do_not_match(self) -> None:
        from hermes.runtime.security_hook import _is_browser_session_tool

        for t in ("web_search", "web_extract", "terminal", "execute_code",
                  "delegate_task", "send_message", "", "browse"):
            assert _is_browser_session_tool(t) is False, t


# ---------------------------------------------------------------------------
# 2. Per-conversation approval state
# ---------------------------------------------------------------------------

class TestBrowserSessionState:
    def _clear(self) -> None:
        from hermes.runtime.security_hook import (
            _browser_approved_conversations, _browser_approved_lock,
        )
        with _browser_approved_lock:
            _browser_approved_conversations.clear()

    def test_mark_and_query(self) -> None:
        from hermes.runtime.security_hook import (
            _browser_session_is_approved, _mark_browser_session_approved,
        )
        self._clear()
        assert _browser_session_is_approved("conv-a") is False
        _mark_browser_session_approved("conv-a")
        assert _browser_session_is_approved("conv-a") is True
        assert _browser_session_is_approved("conv-b") is False
        self._clear()

    def test_empty_conversation_never_approved(self) -> None:
        from hermes.runtime.security_hook import (
            _browser_session_is_approved, _mark_browser_session_approved,
        )
        self._clear()
        _mark_browser_session_approved("")  # no-op
        assert _browser_session_is_approved("") is False
        self._clear()

    def test_bounded_eviction_only_reasks(self) -> None:
        """Exceeding the cap evicts the OLDEST approval (a harmless re-ask), never a
        silent grant; the most-recent approvals survive."""
        from hermes.runtime import security_hook as sh
        self._clear()
        cap = sh._BROWSER_APPROVED_MAX
        for i in range(cap + 10):
            sh._mark_browser_session_approved(f"conv-{i}")
        # oldest 10 evicted, newest cap survive
        assert sh._browser_session_is_approved("conv-0") is False
        assert sh._browser_session_is_approved(f"conv-{cap + 9}") is True
        with sh._browser_approved_lock:
            assert len(sh._browser_approved_conversations) <= cap
        self._clear()


# ---------------------------------------------------------------------------
# 3. _resolve_browser_session_consent (resolver/route/registry mocked)
# ---------------------------------------------------------------------------

class TestResolveBrowserSessionConsent:
    def _clear(self) -> None:
        from hermes.runtime.security_hook import (
            _browser_approved_conversations, _browser_approved_lock,
        )
        with _browser_approved_lock:
            _browser_approved_conversations.clear()

    def _patch_common(self, monkeypatch, conv_id: str) -> None:
        import hermes.runtime.conversation_task_registry as reg
        from hermes.runtime import security_hook as sh
        monkeypatch.setattr(reg, "get_conversation_for_task", lambda _t: conv_id)
        monkeypatch.setattr(reg, "get_current_cycle_agent", lambda: "cerebro")
        monkeypatch.setattr(sh, "_compute_danger_route", lambda *a, **k: (None, frozenset()))

    def test_approve_marks_conversation(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh
        self._clear()
        self._patch_common(monkeypatch, "conv-1")
        captured = {}

        def _fake_resolver(*_a, **kw):
            captured.update(kw)
            return None  # approved

        monkeypatch.setattr(sh, "_resolve_native_danger_approval", _fake_resolver)
        out = sh._resolve_browser_session_consent(
            "browser_navigate", {"url": "https://x"}, "task-1", MagicMock(), MagicMock(),
            None, "tenant",
        )
        assert out is None  # ALLOW
        assert sh._browser_session_is_approved("conv-1") is True
        # resolver got the per-CONVERSATION session key + a browser justification
        assert captured["session_key"] == "browser-session\x00conv-1"
        assert "navegador" in captured["justification_override"].lower()
        self._clear()

    def test_deny_does_not_mark(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh
        self._clear()
        self._patch_common(monkeypatch, "conv-2")
        monkeypatch.setattr(
            sh, "_resolve_native_danger_approval",
            lambda *_a, **_k: "El dueño rechazó la acción 'browser_navigate'. No la reintentes.",
        )
        out = sh._resolve_browser_session_consent(
            "browser_navigate", {"url": "https://x"}, "task-2", MagicMock(), MagicMock(),
            None, "tenant",
        )
        assert out is not None  # BLOCK
        assert sh._browser_session_is_approved("conv-2") is False
        self._clear()

    def test_already_approved_short_circuits(self, monkeypatch) -> None:
        """A conversation already approved returns None WITHOUT calling the resolver
        (no second card) — this is the per-SESSION behaviour."""
        from hermes.runtime import security_hook as sh
        self._clear()
        self._patch_common(monkeypatch, "conv-3")
        sh._mark_browser_session_approved("conv-3")
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)
        out = sh._resolve_browser_session_consent(
            "browser_click", {"x": 1}, "task-3", MagicMock(), MagicMock(), None, "tenant",
        )
        assert out is None
        resolver.assert_not_called()
        self._clear()

    def test_autonomous_no_conversation_is_ungated(self, monkeypatch) -> None:
        """Non-interactive cycle (no conversation → no owner watching) must NOT be gated
        on an interactive card that would time out and deny — it ALLOWs (pre-gate
        behaviour, cage still confines it). No regression for autonomous browsing."""
        from hermes.runtime import security_hook as sh
        self._clear()
        self._patch_common(monkeypatch, "")  # get_conversation_for_task → ""
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)
        out = sh._resolve_browser_session_consent(
            "browser_navigate", {"url": "https://x"}, "task-auto", MagicMock(), MagicMock(),
            None, "tenant",
        )
        assert out is None  # ALLOW, no card
        resolver.assert_not_called()
        self._clear()

    def test_browser_cdp_gets_own_per_action_card(self, monkeypatch) -> None:
        """browser_cdp (arbitrary CDP) is NEVER folded into the session consent: it
        gets its OWN per-action card (no session_key), a justification that mentions
        CDP, and it does NOT grant/consume the session approval — even when the session
        is already approved."""
        from hermes.runtime import security_hook as sh
        self._clear()
        self._patch_common(monkeypatch, "conv-cdp")
        sh._mark_browser_session_approved("conv-cdp")  # session already approved
        captured = {}

        def _fake_resolver(*_a, **kw):
            captured.update(kw)
            return None  # approved

        monkeypatch.setattr(sh, "_resolve_native_danger_approval", _fake_resolver)
        out = sh._resolve_browser_session_consent(
            "browser_cdp", {"cmd": "Runtime.evaluate"}, "task-cdp", MagicMock(), MagicMock(),
            None, "tenant",
        )
        assert out is None  # approved this ONE action
        # per-ACTION: no session_key was passed (defaults to per-action digest)
        assert captured.get("session_key", "") == ""
        # the card copy reveals the CDP scope, not just "navigate/click"
        assert "cdp" in captured["justification_override"].lower()
        self._clear()


# ---------------------------------------------------------------------------
# 4. session_key → per-conversation proposal_id (one card per session)
# ---------------------------------------------------------------------------

def _pid_for_session(session_key: str) -> str:
    d = hashlib.sha256(session_key.encode("utf-8", "replace")).hexdigest()
    return str(uuid5(NAMESPACE_URL, d))


def _pid_for_action(tool: str, args: dict) -> str:
    d = hashlib.sha256(
        (tool + "\x00" + json.dumps(args, sort_keys=True, default=str)).encode("utf-8", "replace")
    ).hexdigest()
    return str(uuid5(NAMESPACE_URL, d))


class TestSessionKeyProposalId:
    """The proposal_id derives from session_key (per-conversation), not (tool+args)."""

    def _run_with_prebuffered_approval(self, *, session_key: str, tool: str, args: dict) -> str:
        """Run _resolve_native_danger_approval, pre-buffering an 'approved' presignal for
        the EXPECTED proposal_id so the gate resumes immediately; return the proposal_id
        the fake gate actually registered."""
        from hermes.runtime.security_hook import (
            _resolve_native_danger_approval, signal_native_danger_approval,
        )
        expected = _pid_for_session(session_key) if session_key else _pid_for_action(tool, args)

        gate = MagicMock()
        gate.register_pending = AsyncMock()
        gate.expire = AsyncMock()
        broker = MagicMock()
        broker._approval_gate = gate

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
        t.start()
        try:
            # Buffer the approval for the id the function is about to register.
            signal_native_danger_approval(expected, "approved")
            result = _resolve_native_danger_approval(
                tool, args, broker, loop,
                conversation_id="conv-x", session_key=session_key,
                justification_override="test",
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
        assert result is None, f"expected ALLOW (approved) got {result!r}"
        registered = str(gate.register_pending.call_args.kwargs["proposal_id"])
        assert registered == expected
        return registered

    def test_same_session_diff_args_same_proposal(self) -> None:
        sk = "browser-session\x00conv-x"
        pid1 = self._run_with_prebuffered_approval(session_key=sk, tool="browser_navigate", args={"url": "a"})
        pid2 = self._run_with_prebuffered_approval(session_key=sk, tool="browser_click", args={"x": 9})
        assert pid1 == pid2, "same conversation must reuse ONE proposal/card"

    def test_no_session_key_uses_action_digest(self) -> None:
        args = {"url": "a"}
        pid_action = self._run_with_prebuffered_approval(session_key="", tool="browser_navigate", args=args)
        assert pid_action == _pid_for_action("browser_navigate", args)
        assert pid_action != _pid_for_session("browser-session\x00conv-x")


# ---------------------------------------------------------------------------
# 5. Browser liveness probe (chip source of truth)
# ---------------------------------------------------------------------------

class TestBrowserControllerSocketGrant:
    """BROWSER_CONTROLLER must grant the browser SOCKET dir (/var/lib/hermes/tmp, where
    the vendored browser_tool creates agent-browser-<session>) so the CDP controller can
    create its Unix socket — the fix for the EACCES that silently fell back to an
    invisible headless browser — while STILL denying the keystore (2026-07-09)."""

    def test_grants_socket_dir_but_denies_keystore(self) -> None:
        from hermes.agents_os.infrastructure.landlock_ruleset_builder import _CAPABILITY_PATHS
        from hermes.agents_os.application.consent_manager import Capability

        paths = {p for p, _ in _CAPABILITY_PATHS[Capability.BROWSER_CONTROLLER]}
        # socket dir + the controller's own session subtree are granted
        assert "/var/lib/hermes/tmp" in paths
        assert "/var/lib/hermes/browser-sessions" in paths
        # crown jewels stay denied — Landlock is path-prefix, so the sibling
        # /var/lib/hermes/master.key is NOT covered by /var/lib/hermes/tmp
        assert "/var/lib/hermes" not in paths, "granting the root would expose master.key"
        assert "/var" not in paths


class TestBrowserLiveness:
    def test_is_real_url(self) -> None:
        from hermes.browser.infrastructure.browser_liveness import _is_real_url

        assert _is_real_url("https://example.com") is True
        assert _is_real_url("http://10.0.0.1/x") is True
        for blank in ("", "about:blank", "chrome://newtab", "devtools://x", "chrome-extension://y"):
            assert _is_real_url(blank) is False, blank

    def test_agent_browser_live_failsoft(self, monkeypatch) -> None:
        """No reachable CDP → False, never raises (best-effort probe)."""
        import hermes.browser.infrastructure.browser_liveness as bl

        # bust the TTL cache and force the probe path to error
        bl._cache["at"] = 0.0
        def _boom(*_a, **_k):
            raise OSError("unreachable")
        monkeypatch.setattr(bl.urllib.request, "urlopen", _boom)
        assert bl.agent_browser_live() is False

    def test_agent_browser_live_true_on_real_page(self, monkeypatch) -> None:
        import hermes.browser.infrastructure.browser_liveness as bl

        class _Resp:
            def __init__(self, body: bytes) -> None:
                self._b = body
            def read(self) -> bytes:
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                return False

        payload = json.dumps([
            {"type": "page", "url": "about:blank"},
            {"type": "page", "url": "https://news.example.com/article"},
        ]).encode()
        bl._cache["at"] = 0.0
        monkeypatch.setattr(bl.urllib.request, "urlopen", lambda *_a, **_k: _Resp(payload))
        assert bl.agent_browser_live() is True

        # only blank/internal pages → not live
        payload_blank = json.dumps([{"type": "page", "url": "about:blank"}]).encode()
        bl._cache["at"] = 0.0
        monkeypatch.setattr(bl.urllib.request, "urlopen", lambda *_a, **_k: _Resp(payload_blank))
        assert bl.agent_browser_live() is False
