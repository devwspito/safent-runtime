"""Tests del InMemoryAuditSink — acumulador para assertions en tests."""

from __future__ import annotations

import pytest

from hermes.egress_proxy.domain.policy import EgressDecision, EgressMode
from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink

pytestmark = pytest.mark.unit


def _allow(domain: str, session_id: str = "sess-1") -> EgressDecision:
    return EgressDecision(
        allowed=True,
        domain=domain,
        session_id=session_id,
        mode=EgressMode.OPEN_LOGGED,
        reason="open-logged: any domain allowed",
    )


def _deny(domain: str, session_id: str = "sess-1") -> EgressDecision:
    return EgressDecision(
        allowed=False,
        domain=domain,
        session_id=session_id,
        mode=EgressMode.DEFAULT_DENY,
        reason="default-deny: domain not in whitelist",
    )


class TestInMemoryAuditSink:
    def test_empty_at_start(self) -> None:
        sink = InMemoryAuditSink()
        assert sink.decisions == []
        assert sink.allowed_domains() == []
        assert sink.denied_domains() == []

    def test_records_allow(self) -> None:
        sink = InMemoryAuditSink()
        sink.record(_allow("example.com"))
        assert "example.com" in sink.allowed_domains()
        assert sink.denied_domains() == []

    def test_records_deny(self) -> None:
        sink = InMemoryAuditSink()
        sink.record(_deny("evil.com"))
        assert "evil.com" in sink.denied_domains()
        assert sink.allowed_domains() == []

    def test_multiple_decisions(self) -> None:
        sink = InMemoryAuditSink()
        sink.record(_allow("example.com"))
        sink.record(_deny("evil.com"))
        sink.record(_allow("cdn.example.com"))
        assert len(sink.decisions) == 3
        assert set(sink.allowed_domains()) == {"example.com", "cdn.example.com"}
        assert sink.denied_domains() == ["evil.com"]
