"""TailnetHost — format validation: never an IP, never a URL/scheme."""

from __future__ import annotations

import pytest

from hermes.tailnet_ssh.domain.errors import InvalidTailnetHostError
from hermes.tailnet_ssh.domain.host import TailnetHost

pytestmark = pytest.mark.unit


class TestTailnetHostAcceptsValidNames:
    def test_accepts_fqdn(self) -> None:
        assert TailnetHost.parse("db1.tailxxxx.ts.net").value == "db1.tailxxxx.ts.net"

    def test_accepts_bare_short_name(self) -> None:
        assert TailnetHost.parse("db1").value == "db1"

    def test_lowercases(self) -> None:
        assert TailnetHost.parse("DB1.TailXXXX.ts.NET").value == "db1.tailxxxx.ts.net"

    def test_strips_whitespace(self) -> None:
        assert TailnetHost.parse("  db1  ").value == "db1"


class TestTailnetHostRejectsIpLiterals:
    @pytest.mark.parametrize(
        "raw",
        [
            "10.0.0.1",
            "127.0.0.1",
            "100.64.1.2",  # tailscale's own CGNAT range — still rejected
            "0.0.0.0",
            "255.255.255.255",
            "::1",
            "fe80::1",
            "[::1]",
            "2001:db8::1",
        ],
    )
    def test_rejects_ip(self, raw: str) -> None:
        with pytest.raises(InvalidTailnetHostError):
            TailnetHost.parse(raw)


class TestTailnetHostRejectsMalformed:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "ssh://db1.tailxxxx.ts.net",
            "db1.tailxxxx.ts.net:22",
            "db1 .tailxxxx.ts.net",
            "db1/../etc",
            "user@db1.tailxxxx.ts.net",
            "-db1.tailxxxx.ts.net",
            "db1..tailxxxx.ts.net",
            "a" * 300,
        ],
    )
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(InvalidTailnetHostError):
            TailnetHost.parse(raw)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(InvalidTailnetHostError):
            TailnetHost.parse(12345)  # type: ignore[arg-type]
