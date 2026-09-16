"""Integration-style test: ProxyConnectionHandler + UpstreamRouter end-to-end.

A fake HTTP-CONNECT proxy listens on a random loopback port, standing in for
tailscaled's ``127.0.0.1:1055``. Proves:
  - a CONNECT to a host under the MagicDNS suffix is routed to the fake
    tailnet proxy (it receives the CONNECT, the client gets 200);
  - a CONNECT to a host OUTSIDE the suffix NEVER touches the fake tailnet
    proxy — it takes the direct path (stubbed, no real network).

No parallel allowlist: the policy engine still evaluates every host through
EgressPolicyEngine exactly as before — the router only decides HOW to dial
an ALREADY-allowed host, never whether it's allowed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hermes.egress_proxy.domain.policy import (
    EgressMode,
    EgressPolicyEngine,
    SessionPolicy,
)
from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink
from hermes.egress_proxy.infrastructure.proxy_handler import (
    DirectUpstreamConnector,
    ProxyConnectionHandler,
)
from hermes.egress_proxy.infrastructure.tailnet_connector import (
    MagicDnsSuffixSource,
    TailnetUpstreamConnector,
    UpstreamRouter,
)

pytestmark = pytest.mark.unit


class _CollectingTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    def get_extra_info(self, name: str, default=None):  # type: ignore[override]
        if name == "peername":
            return ("10.200.0.2", 54321)
        return default


def _make_writer(transport: _CollectingTransport) -> asyncio.StreamWriter:
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    return asyncio.StreamWriter(transport, protocol, asyncio.StreamReader(), asyncio.get_event_loop())


def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _start_fake_tailnet_proxy() -> tuple[asyncio.AbstractServer, list[str], int]:
    """Fake stand-in for tailscaled's loopback CONNECT proxy. Records CONNECT hosts."""
    received_hosts: list[str] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode("ascii", "replace").strip()
        received_hosts.append(line)
        while True:
            h = await reader.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    server = await asyncio.start_server(_handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    return server, received_hosts, port


def _build_router(status_path: Path, tailnet_proxy_port: int) -> UpstreamRouter:
    return UpstreamRouter(
        suffix_source=MagicDnsSuffixSource(status_path),
        tailnet_connector=TailnetUpstreamConnector(
            proxy_host="127.0.0.1", proxy_port=tailnet_proxy_port
        ),
        default_connector=DirectUpstreamConnector(),
    )


@pytest.mark.asyncio
async def test_suffix_matched_host_is_routed_to_the_tailnet_proxy(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
    fake_proxy, received_hosts, proxy_port = await _start_fake_tailnet_proxy()

    engine = EgressPolicyEngine(
        global_policy=SessionPolicy(
            session_id="__global__",
            mode=EgressMode.DEFAULT_DENY,
            domains_whitelist=frozenset({"myhost.tail1234.ts.net"}),
        )
    )
    sink = InMemoryAuditSink()
    handler = ProxyConnectionHandler(
        policy_engine=engine,
        audit_sink=sink,
        upstream_connector=_build_router(status_path, proxy_port),
    )

    transport = _CollectingTransport()
    request = b"CONNECT myhost.tail1234.ts.net:443 HTTP/1.1\r\n\r\n"
    reader = _make_reader(request + _tls_client_hello("myhost.tail1234.ts.net"))
    writer = _make_writer(transport)

    try:
        await handler.handle(reader, writer)
    finally:
        fake_proxy.close()

    assert received_hosts == ["CONNECT myhost.tail1234.ts.net:443 HTTP/1.1"]
    assert "myhost.tail1234.ts.net" in sink.allowed_domains()
    assert b"200" in bytes(transport.written)


@pytest.mark.asyncio
async def test_non_suffix_host_never_touches_the_tailnet_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"magicdns_suffix": "tail1234.ts.net"}))
    fake_proxy, received_hosts, proxy_port = await _start_fake_tailnet_proxy()

    # Stub the DIRECT path's network calls (no real internet in unit tests) —
    # mirrors the existing test_proxy_handler.py convention.
    async def _fake_resolve_external_ips(host: str):
        return ["203.0.113.10"]

    async def _fake_open_connection(host, port):
        remote_reader = asyncio.StreamReader()
        remote_reader.feed_eof()
        return remote_reader, _make_writer(_CollectingTransport())

    monkeypatch.setattr(
        "hermes.egress_proxy.infrastructure.proxy_handler._resolve_external_ips",
        _fake_resolve_external_ips,
    )
    monkeypatch.setattr(
        "hermes.egress_proxy.infrastructure.proxy_handler.asyncio.open_connection",
        _fake_open_connection,
    )

    engine = EgressPolicyEngine(
        global_policy=SessionPolicy(
            session_id="__global__",
            mode=EgressMode.OPEN_LOGGED,
        )
    )
    sink = InMemoryAuditSink()
    handler = ProxyConnectionHandler(
        policy_engine=engine,
        audit_sink=sink,
        upstream_connector=_build_router(status_path, proxy_port),
    )

    transport = _CollectingTransport()
    request = b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
    reader = _make_reader(request)
    writer = _make_writer(transport)

    try:
        await handler.handle(reader, writer)
    finally:
        fake_proxy.close()

    assert received_hosts == []  # the fake tailnet proxy was NEVER contacted
    assert "example.com" in sink.allowed_domains()
    assert b"200" in bytes(transport.written)


def _tls_client_hello(sni: str) -> bytes:
    """Minimal TLS 1.2 ClientHello carrying ``sni`` (same construction as
    test_proxy_handler.py — DEFAULT_DENY needs the SNI to clear Fix-4)."""
    import struct

    sni_bytes = sni.encode("ascii")
    sni_entry = struct.pack(">BH", 0x00, len(sni_bytes)) + sni_bytes
    sni_list = struct.pack(">H", len(sni_entry)) + sni_entry
    sni_ext = struct.pack(">HH", 0x0000, len(sni_list)) + sni_list
    exts_block = struct.pack(">H", len(sni_ext)) + sni_ext
    ch_body = (
        b"\x03\x03" + b"\x00" * 32 + b"\x00"
        + b"\x00\x02" + b"\xc0\x2b"
        + b"\x01" + b"\x00"
        + exts_block
    )
    hs = struct.pack(">B", 0x01) + struct.pack(">I", len(ch_body))[1:] + ch_body
    return struct.pack(">BHH", 0x16, 0x0303, len(hs)) + hs
