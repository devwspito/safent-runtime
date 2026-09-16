"""Per-HOST tailnet SSH consent gate (spec 022 v2) — mirrors
`test_browser_session_consent.py`'s structure for the sibling always-ask
gate, but PERSISTENT-per-host instead of per-conversation.

These tests lock:
1. `_is_tailnet_ssh_tool` classification (single source: tool_names).
2. Invalid/unknown host: rejected before any card (resolver never called).
3. Already-allowed host: ALLOW, no card, resolver never called.
4. First use: card shown; approve persists the host to the allow-list; deny
   does NOT persist.
5. Autonomous cycles (no conversation): fail-CLOSED for a never-approved
   host (the opposite of the browser gate's fail-open — see module
   docstring on `_resolve_tailnet_ssh_consent`); an ALREADY-allowed host
   still flows.
6. session_key is keyed by the CANONICAL host, not the raw arg spelling.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


class _FakeDirectory:
    def __init__(self, status) -> None:
        self._status = status

    def read(self):
        return self._status


class _FakeAllowlist:
    def __init__(self) -> None:
        self.allowed: set[str] = set()
        self.allow_calls: list[str] = []

    def is_allowed(self, host: str) -> bool:
        return host in self.allowed

    def allow(self, host: str) -> None:
        self.allow_calls.append(host)
        self.allowed.add(host)


def _status():
    from hermes.tailnet_ssh.application.ports import TailnetPeer, TailnetStatus

    return TailnetStatus(
        node_name="safent-agent", magicdns_suffix="tailxxxx.ts.net", online=True,
        peers=(TailnetPeer(name="db1", online=True),),
    )


def _patch_infra(monkeypatch, *, status=None, allowlist: _FakeAllowlist | None = None):
    import hermes.tailnet_ssh.infrastructure.json_host_allowlist_store as allowlist_mod
    import hermes.tailnet_ssh.infrastructure.status_json_directory as directory_mod

    resolved_status = status if status is not None else _status()
    fake_allowlist = allowlist if allowlist is not None else _FakeAllowlist()
    monkeypatch.setattr(
        directory_mod, "StatusJsonTailnetDirectory",
        lambda: _FakeDirectory(resolved_status),
    )
    monkeypatch.setattr(allowlist_mod, "JsonHostAllowlistStore", lambda: fake_allowlist)
    return fake_allowlist


def _patch_conversation(monkeypatch, conv_id: str) -> None:
    import hermes.runtime.conversation_task_registry as reg

    monkeypatch.setattr(reg, "get_conversation_for_task", lambda _t: conv_id)
    monkeypatch.setattr(reg, "get_current_cycle_agent", lambda: "cerebro")


class TestIsTailnetSshTool:
    def test_matches_all_three_tools(self) -> None:
        from hermes.runtime.security_hook import _is_tailnet_ssh_tool

        for t in ("tailnet_ssh", "tailnet_file_get", "tailnet_file_put"):
            assert _is_tailnet_ssh_tool(t) is True, t

    def test_does_not_match_unrelated_tools(self) -> None:
        from hermes.runtime.security_hook import _is_tailnet_ssh_tool

        for t in ("terminal", "browser_navigate", "send_message", "", "tailnet"):
            assert _is_tailnet_ssh_tool(t) is False, t


class TestResolveTailnetSshConsentValidation:
    def test_invalid_host_blocks_without_a_card(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh

        _patch_infra(monkeypatch)
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "10.0.0.1", "command": "ls"}, "task-1",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is not None
        resolver.assert_not_called()

    def test_unknown_host_blocks_without_a_card(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh

        _patch_infra(monkeypatch)
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "evil.com", "command": "ls"}, "task-1",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is not None
        resolver.assert_not_called()


class TestResolveTailnetSshConsentAlreadyAllowed:
    def test_already_allowed_host_flows_without_a_card(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh

        allowlist = _FakeAllowlist()
        allowlist.allow("db1.tailxxxx.ts.net")
        _patch_infra(monkeypatch, allowlist=allowlist)
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "db1", "command": "uptime"}, "task-1",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is None
        resolver.assert_not_called()


class TestResolveTailnetSshConsentFirstUse:
    def test_approve_persists_the_canonical_host(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh

        allowlist = _patch_infra(monkeypatch)
        _patch_conversation(monkeypatch, "conv-1")
        monkeypatch.setattr(sh, "_compute_danger_route", lambda *_a, **_k: (None, frozenset()))
        captured = {}

        def _fake_resolver(*_a, **kw):
            captured.update(kw)

        monkeypatch.setattr(sh, "_resolve_native_danger_approval", _fake_resolver)

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "db1", "command": "uptime"}, "task-1",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is None
        assert allowlist.allow_calls == ["db1.tailxxxx.ts.net"]
        assert captured["session_key"] == "tailnet-ssh\x00db1.tailxxxx.ts.net"
        assert "ssh" in captured["justification_override"].lower()

    def test_deny_does_not_persist(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh

        allowlist = _patch_infra(monkeypatch)
        _patch_conversation(monkeypatch, "conv-2")
        monkeypatch.setattr(sh, "_compute_danger_route", lambda *_a, **_k: (None, frozenset()))
        monkeypatch.setattr(
            sh, "_resolve_native_danger_approval",
            lambda *_a, **_k: "El dueño rechazó la acción 'tailnet_ssh'. No la reintentes.",
        )

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "db1", "command": "uptime"}, "task-2",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is not None
        assert allowlist.allow_calls == []

    def test_raw_arg_spelling_resolves_to_the_same_canonical_session_key(self, monkeypatch) -> None:
        """`host="db1"` and `host="db1.tailxxxx.ts.net"` must key the SAME
        card — otherwise the owner would be asked twice for the same host."""
        from hermes.runtime import security_hook as sh

        _patch_infra(monkeypatch)
        _patch_conversation(monkeypatch, "conv-3")
        monkeypatch.setattr(sh, "_compute_danger_route", lambda *_a, **_k: (None, frozenset()))
        captured = {}
        monkeypatch.setattr(
            sh, "_resolve_native_danger_approval",
            lambda *_a, **kw: (captured.update(kw), None)[1],
        )

        sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "db1.tailxxxx.ts.net", "command": "uptime"}, "task-3",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert captured["session_key"] == "tailnet-ssh\x00db1.tailxxxx.ts.net"


class TestResolveTailnetSshConsentAutonomousCycles:
    def test_new_host_fails_closed_without_a_conversation(self, monkeypatch) -> None:
        """Deliberately the OPPOSITE of the browser session gate: tailnet_ssh
        has no other governing floor, so an unattended cycle must never be
        the first to reach a brand-new host."""
        from hermes.runtime import security_hook as sh

        _patch_infra(monkeypatch)
        _patch_conversation(monkeypatch, "")  # no conversation
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "db1", "command": "uptime"}, "task-auto",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is not None
        resolver.assert_not_called()

    def test_already_allowed_host_flows_without_a_conversation(self, monkeypatch) -> None:
        from hermes.runtime import security_hook as sh

        allowlist = _FakeAllowlist()
        allowlist.allow("db1.tailxxxx.ts.net")
        _patch_infra(monkeypatch, allowlist=allowlist)
        _patch_conversation(monkeypatch, "")
        resolver = MagicMock()
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", resolver)

        out = sh._resolve_tailnet_ssh_consent(
            "tailnet_ssh", {"host": "db1", "command": "uptime"}, "task-auto",
            MagicMock(), MagicMock(), None, "tenant",
        )

        assert out is None
        resolver.assert_not_called()
