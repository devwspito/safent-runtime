"""Tests del conector de egress hacia la tailnet gobernada (spec 022).

Cobertura:
  - MagicDnsSuffixSource: fichero ausente/corrupto/campo inválido → None
    (fail-closed); lectura válida; cache por mtime (no relee si no cambió,
    relee si cambió).
  - host_matches_suffix: sufijo exacto, subdominio, no-match, literal IP
    nunca coincide (v1 = FQDN-only).
  - TailnetUpstreamConnector: framing CONNECT correcto, 200 → streams
    utilizables, no-200 → UpstreamConnectError, fallo de dial →
    UpstreamConnectError.
  - UpstreamRouter: host en el sufijo → tailnet_connector; host fuera →
    default_connector; sin sufijo (tailnet no configurada) → default_connector.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hermes.egress_proxy.application.ports import UpstreamConnectError
from hermes.egress_proxy.infrastructure.tailnet_connector import (
    MagicDnsSuffixSource,
    TailnetUpstreamConnector,
    UpstreamRouter,
    host_matches_suffix,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# MagicDnsSuffixSource
# ---------------------------------------------------------------------------


class TestMagicDnsSuffixSource:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        source = MagicDnsSuffixSource(tmp_path / "absent-status.json")
        assert source.current_suffix() is None

    def test_valid_file_returns_suffix(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() == "tail1234.ts.net"

    def test_suffix_normalized_lowercase_no_trailing_dot(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "TAIL1234.TS.NET."}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() == "tail1234.ts.net"

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text("{not valid json")
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() is None

    def test_missing_field_returns_none(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"node_name": "agent", "online": True}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() is None

    def test_non_string_field_returns_none(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": 12345}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() is None

    def test_empty_string_field_returns_none(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "   "}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() is None

    def test_cache_not_reread_when_mtime_unchanged(self, tmp_path: Path) -> None:
        """Black-box proof of caching: overwrite content but PIN the mtime — the
        stale (cached) value must still be returned, proving no re-read happened."""
        import os

        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() == "tail1234.ts.net"

        pinned_mtime = os.stat(status).st_mtime
        status.write_text(json.dumps({"magicdns_suffix": "changed-tailnet.ts.net"}))
        os.utime(status, (pinned_mtime, pinned_mtime))

        assert source.current_suffix() == "tail1234.ts.net"  # cache hit, not re-read

    def test_cache_invalidated_on_mtime_change(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "old-tailnet.ts.net"}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() == "old-tailnet.ts.net"

        # Force a distinct mtime (filesystem mtime resolution can be coarse).
        import os
        import time

        new_content = json.dumps({"magicdns_suffix": "new-tailnet.ts.net"})
        status.write_text(new_content)
        future = os.stat(status).st_mtime + 5.0
        os.utime(status, (future, future))

        assert source.current_suffix() == "new-tailnet.ts.net"

    def test_file_disappearing_invalidates_cache(self, tmp_path: Path) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
        source = MagicDnsSuffixSource(status)
        assert source.current_suffix() == "tail1234.ts.net"

        status.unlink()
        assert source.current_suffix() is None


# ---------------------------------------------------------------------------
# host_matches_suffix
# ---------------------------------------------------------------------------


class TestHostMatchesSuffix:
    def test_exact_suffix_matches(self) -> None:
        assert host_matches_suffix("tail1234.ts.net", "tail1234.ts.net")

    def test_subdomain_matches(self) -> None:
        assert host_matches_suffix("myhost.tail1234.ts.net", "tail1234.ts.net")

    def test_unrelated_host_does_not_match(self) -> None:
        assert not host_matches_suffix("evil.com", "tail1234.ts.net")

    def test_suffix_as_substring_but_not_subdomain_does_not_match(self) -> None:
        # "nottail1234.ts.net" ends with "tail1234.ts.net" as a raw string but
        # is NOT a subdomain (no dot boundary) — must be rejected.
        assert not host_matches_suffix("nottail1234.ts.net", "tail1234.ts.net")

    def test_case_insensitive(self) -> None:
        assert host_matches_suffix("MyHost.Tail1234.TS.NET", "tail1234.ts.net")

    def test_ip_literal_never_matches(self) -> None:
        assert not host_matches_suffix("100.64.1.5", "100.64.1.5")

    def test_ipv6_literal_never_matches(self) -> None:
        assert not host_matches_suffix("fd7a:115c:a1e0::1", "fd7a:115c:a1e0::1")

    def test_empty_suffix_never_matches(self) -> None:
        assert not host_matches_suffix("myhost.tail1234.ts.net", "")


# ---------------------------------------------------------------------------
# TailnetUpstreamConnector — CONNECT framing over a fake loopback proxy
# ---------------------------------------------------------------------------


async def _start_fake_connect_proxy(
    *, respond_ok: bool = True
) -> tuple[asyncio.AbstractServer, list[bytes], int]:
    """Start a minimal TCP server that speaks just enough CONNECT to test framing.

    Records the raw CONNECT request line it received; replies 200 or 403.
    Returns (server, received_lines, port).
    """
    received: list[bytes] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        received.append(line)
        # Drain headers until blank line.
        while True:
            h = await reader.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
        if respond_ok:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await writer.drain()
        # Keep the connection open briefly so the client can read past the status line.
        await asyncio.sleep(0.05)
        writer.close()

    server = await asyncio.start_server(_handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    return server, received, port


class TestTailnetUpstreamConnector:
    @pytest.mark.asyncio
    async def test_connect_sends_correct_connect_line(self) -> None:
        server, received, port = await _start_fake_connect_proxy(respond_ok=True)
        async with server:
            connector = TailnetUpstreamConnector(proxy_host="127.0.0.1", proxy_port=port)
            reader, writer = await connector.connect(
                host="myhost.tail1234.ts.net", port=443, timeout=2.0
            )
            writer.close()
        assert received == [b"CONNECT myhost.tail1234.ts.net:443 HTTP/1.1\r\n"]

    @pytest.mark.asyncio
    async def test_non_200_raises_upstream_connect_error(self) -> None:
        server, _received, port = await _start_fake_connect_proxy(respond_ok=False)
        async with server:
            connector = TailnetUpstreamConnector(proxy_host="127.0.0.1", proxy_port=port)
            with pytest.raises(UpstreamConnectError):
                await connector.connect(host="denied.tail1234.ts.net", port=443, timeout=2.0)

    @pytest.mark.asyncio
    async def test_dial_failure_raises_upstream_connect_error(self) -> None:
        # No server listening on this port — connection must be refused.
        connector = TailnetUpstreamConnector(proxy_host="127.0.0.1", proxy_port=1)
        with pytest.raises(UpstreamConnectError):
            await connector.connect(host="myhost.tail1234.ts.net", port=443, timeout=1.0)

    @pytest.mark.asyncio
    async def test_timeout_raises_upstream_connect_error(self) -> None:
        async def _hang(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            await asyncio.sleep(10)  # never responds within the test's timeout

        server = await asyncio.start_server(_hang, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        try:
            connector = TailnetUpstreamConnector(proxy_host="127.0.0.1", proxy_port=port)
            with pytest.raises(UpstreamConnectError):
                await connector.connect(host="slow.tail1234.ts.net", port=443, timeout=0.05)
        finally:
            # NOT `async with server` / `wait_closed()`: the still-sleeping _hang
            # handler task keeps the server "open" from asyncio's point of view
            # (Server.wait_closed() waits for active connections on 3.12+) and
            # would hang the test. close() alone is enough — the test process
            # ends right after, so the orphaned handler task is reaped for free.
            server.close()


# ---------------------------------------------------------------------------
# UpstreamRouter — selection logic
# ---------------------------------------------------------------------------


class _FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def connect(self, *, host: str, port: int, timeout: float):
        self.calls.append((host, port))
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, _FakeWriter()


class _FakeWriter:
    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False


class TestUpstreamRouter:
    @pytest.mark.asyncio
    async def test_suffix_matched_host_routes_to_tailnet_connector(
        self, tmp_path: Path
    ) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
        tailnet = _FakeConnector()
        default = _FakeConnector()
        router = UpstreamRouter(
            suffix_source=MagicDnsSuffixSource(status),
            tailnet_connector=tailnet,
            default_connector=default,
        )
        await router.connect(host="myhost.tail1234.ts.net", port=443, timeout=2.0)
        assert tailnet.calls == [("myhost.tail1234.ts.net", 443)]
        assert default.calls == []

    @pytest.mark.asyncio
    async def test_non_suffix_host_routes_to_default_connector(
        self, tmp_path: Path
    ) -> None:
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
        tailnet = _FakeConnector()
        default = _FakeConnector()
        router = UpstreamRouter(
            suffix_source=MagicDnsSuffixSource(status),
            tailnet_connector=tailnet,
            default_connector=default,
        )
        await router.connect(host="example.com", port=443, timeout=2.0)
        assert default.calls == [("example.com", 443)]
        assert tailnet.calls == []

    @pytest.mark.asyncio
    async def test_tailnet_not_configured_routes_everything_to_default(
        self, tmp_path: Path
    ) -> None:
        # No status.json at all — tailnet not configured.
        tailnet = _FakeConnector()
        default = _FakeConnector()
        router = UpstreamRouter(
            suffix_source=MagicDnsSuffixSource(tmp_path / "absent.json"),
            tailnet_connector=tailnet,
            default_connector=default,
        )
        await router.connect(host="myhost.tail1234.ts.net", port=443, timeout=2.0)
        assert default.calls == [("myhost.tail1234.ts.net", 443)]
        assert tailnet.calls == []
