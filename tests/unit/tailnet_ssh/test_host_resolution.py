"""resolve_host — MagicDNS suffix OR listed peer name, never anything else."""

from __future__ import annotations

import pytest

from hermes.tailnet_ssh.application.host_resolution import resolve_host
from hermes.tailnet_ssh.application.ports import TailnetPeer, TailnetStatus
from hermes.tailnet_ssh.domain.errors import InvalidTailnetHostError, UnknownTailnetHostError

pytestmark = pytest.mark.unit

_STATUS = TailnetStatus(
    node_name="safent-agent",
    magicdns_suffix="tailxxxx.ts.net",
    online=True,
    peers=(
        TailnetPeer(name="db1", online=True),
        TailnetPeer(name="build-box", online=False),
    ),
)


class TestResolveHostBySuffix:
    def test_fqdn_under_suffix_resolves(self) -> None:
        assert resolve_host("db1.tailxxxx.ts.net", _STATUS).value == "db1.tailxxxx.ts.net"

    def test_unlisted_fqdn_under_suffix_still_resolves(self) -> None:
        """Any FQDN under the tailnet's OWN suffix is trusted — the suffix
        itself is the boundary, not membership in the peers snapshot (which
        can lag a freshly-joined node)."""
        assert resolve_host("new-node.tailxxxx.ts.net", _STATUS).value == (
            "new-node.tailxxxx.ts.net"
        )


class TestResolveHostByPeerName:
    def test_bare_peer_name_resolves_to_canonical_fqdn(self) -> None:
        assert resolve_host("db1", _STATUS).value == "db1.tailxxxx.ts.net"

    def test_offline_peer_still_resolves(self) -> None:
        """online=False only informs UX; a stale snapshot must not silently
        deny a legitimate host — the ssh attempt itself is the real check."""
        assert resolve_host("build-box", _STATUS).value == "build-box.tailxxxx.ts.net"

    def test_peer_name_case_insensitive(self) -> None:
        assert resolve_host("DB1", _STATUS).value == "db1.tailxxxx.ts.net"


class TestResolveHostRejectsOutsideTailnet:
    def test_unrelated_domain_rejected(self) -> None:
        with pytest.raises(UnknownTailnetHostError):
            resolve_host("evil.com", _STATUS)

    def test_unlisted_bare_name_rejected(self) -> None:
        with pytest.raises(UnknownTailnetHostError):
            resolve_host("not-a-peer", _STATUS)

    def test_bare_suffix_rejected(self) -> None:
        with pytest.raises(UnknownTailnetHostError):
            resolve_host("tailxxxx.ts.net", _STATUS)

    def test_ip_literal_rejected_before_membership_check(self) -> None:
        with pytest.raises(InvalidTailnetHostError):
            resolve_host("100.64.1.2", _STATUS)

    def test_suffix_substring_but_not_dot_boundary_rejected(self) -> None:
        """`eviltailxxxx.ts.net` must NOT match via a naive substring/endswith
        on the bare suffix — only a `.`-delimited subdomain counts."""
        with pytest.raises(UnknownTailnetHostError):
            resolve_host("eviltailxxxx.ts.net", _STATUS)
