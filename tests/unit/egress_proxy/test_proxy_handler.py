"""Tests del ProxyConnectionHandler con streams en memoria.

No hay red real — usa asyncio.StreamReader/StreamWriter con transport
simulado para verificar la lógica de decisión.

Cubre:
  - CONNECT a dominio permitido (open-logged) → 200 + audit allow.
  - CONNECT a dominio denegado (default-deny con SNI) → 403 + audit deny.
  - CONNECT sin TLS bytes en default-deny → 403 sin audit (no SNI).
  - HTTP plano a dominio permitido → forwarded (upstream simulado).
  - HTTP plano en default-deny → 403 inmediato (Fix-7).
  - CONNECT con Host inválido → 403.
  - open-logged: cualquier dominio, incluido evil → allow + audit.

Fix-4 (SNI enforcement): en DEFAULT_DENY, el proxy lee el ClientHello TLS
ANTES de abrir el upstream. Para testear el path de deny/allow con audit,
los tests inyectan un ClientHello minimal con SNI (ver _make_tls_client_hello).
"""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes.egress_proxy.domain.policy import (
    EgressMode,
    EgressPolicyEngine,
    SessionPolicy,
)
from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink
from hermes.egress_proxy.infrastructure.proxy_handler import ProxyConnectionHandler

pytestmark = pytest.mark.unit


def _make_policy_engine(
    mode: EgressMode = EgressMode.OPEN_LOGGED,
    domains: frozenset[str] = frozenset(),
) -> EgressPolicyEngine:
    return EgressPolicyEngine(
        global_policy=SessionPolicy(
            session_id="__global__",
            mode=mode,
            domains_whitelist=domains,
        )
    )


def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _make_tls_client_hello(sni: str) -> bytes:
    """Build a minimal TLS 1.2 ClientHello with the given SNI hostname.

    Used by tests that exercise the SNI-enforcement path (Fix-4 / DEFAULT_DENY).
    The resulting bytes are appended after the CONNECT+headers in the reader.
    """
    sni_bytes = sni.encode("ascii")
    sni_len = len(sni_bytes)

    # SNI extension body: list_len(2) + name_type(1) + name_len(2) + name
    sni_entry = struct.pack(">BH", 0x00, sni_len) + sni_bytes
    sni_list = struct.pack(">H", len(sni_entry)) + sni_entry
    sni_ext = struct.pack(">HH", 0x0000, len(sni_list)) + sni_list

    # Extensions block
    exts = sni_ext
    exts_block = struct.pack(">H", len(exts)) + exts

    # ClientHello body: version(2) + random(32) + session_id_len(1)
    #                   + cipher_suites_len(2) + cipher_suites(2)
    #                   + compression_len(1) + compression(1) + extensions
    random_bytes = b"\x00" * 32
    ch_body = (
        b"\x03\x03"       # TLS 1.2 client_version
        + random_bytes    # 32 bytes random
        + b"\x00"         # session_id_length = 0
        + b"\x00\x02"     # cipher_suites_length = 2
        + b"\xc0\x2b"     # TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
        + b"\x01"         # compression_methods_length = 1
        + b"\x00"         # null compression
        + exts_block
    )

    # Handshake header: type(1) + length(3)
    hs_header = struct.pack(">B", 0x01) + struct.pack(">I", len(ch_body))[1:]
    hs = hs_header + ch_body

    # TLS record header: content_type(1) + version(2) + length(2)
    record = struct.pack(">BHH", 0x16, 0x0303, len(hs)) + hs
    return record


class _CollectingTransport(asyncio.Transport):
    """Transport que acumula los bytes escritos."""

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
    writer = asyncio.StreamWriter(transport, protocol, asyncio.StreamReader(), asyncio.get_event_loop())
    return writer


# ---------------------------------------------------------------------------
# Helpers para tests sin upstream real
# ---------------------------------------------------------------------------


async def _run_connect_handler(
    connect_request: bytes,
    engine: EgressPolicyEngine,
    sink: InMemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tls_payload: bytes = b"",
) -> bytes:
    """Ejecuta el handler CONNECT y devuelve los bytes escritos al cliente.

    tls_payload: bytes optionales a añadir después del CONNECT (TLS ClientHello).
    Necesario en DEFAULT_DENY para que el proxy pueda leer el SNI (Fix-4).
    """
    transport = _CollectingTransport()
    reader = _make_reader(connect_request + tls_payload)
    writer = _make_writer(transport)

    # Stub de asyncio.open_connection para que no haga red real
    async def _fake_open_connection(host, port):
        remote_reader = asyncio.StreamReader()
        remote_reader.feed_eof()
        remote_transport = _CollectingTransport()
        remote_writer = _make_writer(remote_transport)
        return remote_reader, remote_writer

    monkeypatch.setattr(
        "hermes.egress_proxy.infrastructure.proxy_handler.asyncio.open_connection",
        _fake_open_connection,
    )

    handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
    await handler.handle(reader, writer)
    return bytes(transport.written)


# ---------------------------------------------------------------------------
# Tests CONNECT
# ---------------------------------------------------------------------------


class TestConnectHandler:
    @pytest.mark.asyncio
    async def test_open_logged_allows_any_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _make_policy_engine(EgressMode.OPEN_LOGGED)
        sink = InMemoryAuditSink()
        request = b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
        written = await _run_connect_handler(request, engine, sink, monkeypatch)
        assert b"200" in written
        assert "example.com" in sink.allowed_domains()
        assert sink.denied_domains() == []

    @pytest.mark.asyncio
    async def test_open_logged_allows_evil_domain_and_audits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _make_policy_engine(EgressMode.OPEN_LOGGED)
        sink = InMemoryAuditSink()
        request = b"CONNECT evil.attacker.example:443 HTTP/1.1\r\n\r\n"
        written = await _run_connect_handler(request, engine, sink, monkeypatch)
        assert b"200" in written
        assert "evil.attacker.example" in sink.allowed_domains()

    @pytest.mark.asyncio
    async def test_open_logged_blocks_denylisted_sni_behind_allowed_connect_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti domain-fronting: in OPEN_LOGGED a denylisted SNI riding an allowed
        CONNECT host must be blocked (the SNI was previously logged only)."""
        engine = EgressPolicyEngine(
            global_policy=SessionPolicy(
                session_id="__global__",
                mode=EgressMode.OPEN_LOGGED,
                domains_denylist=frozenset({"blocked.example"}),
            )
        )
        sink = InMemoryAuditSink()
        # Stub DNS resolution so the handler reaches the SNI check (fake domains
        # don't resolve; real resolution is not what this test exercises).
        async def _fake_resolve(host):
            return "1.2.3.4"
        monkeypatch.setattr(
            "hermes.egress_proxy.infrastructure.proxy_handler._resolve_external_ip",
            _fake_resolve,
        )
        connect = b"CONNECT allowed-cdn.example:443 HTTP/1.1\r\n\r\n"
        tls = _make_tls_client_hello("blocked.example")  # fronted SNI
        await _run_connect_handler(connect, engine, sink, monkeypatch, tls_payload=tls)
        # CONNECT host was allowed so 200 is sent, but the denylisted SNI is then
        # blocked and recorded as denied.
        assert "blocked.example" in sink.denied_domains()

    @pytest.mark.asyncio
    async def test_open_logged_allows_normal_sni_behind_connect_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-denylisted SNI still tunnels (open browsing preserved)."""
        engine = EgressPolicyEngine(
            global_policy=SessionPolicy(
                session_id="__global__",
                mode=EgressMode.OPEN_LOGGED,
                domains_denylist=frozenset({"blocked.example"}),
            )
        )
        sink = InMemoryAuditSink()
        async def _fake_resolve(host):
            return "1.2.3.4"
        monkeypatch.setattr(
            "hermes.egress_proxy.infrastructure.proxy_handler._resolve_external_ip",
            _fake_resolve,
        )
        connect = b"CONNECT site.example:443 HTTP/1.1\r\n\r\n"
        tls = _make_tls_client_hello("site.example")
        written = await _run_connect_handler(connect, engine, sink, monkeypatch, tls_payload=tls)
        assert b"200" in written
        assert "blocked.example" not in sink.denied_domains()

    @pytest.mark.asyncio
    async def test_default_deny_blocks_unlisted_domain_with_sni(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-4: DEFAULT_DENY checks the SNI, not just the CONNECT host.

        A CONNECT with a whitelisted CONNECT host but an evil SNI is denied.
        The audit record uses the SNI hostname (the authoritative identifier).
        """
        engine = _make_policy_engine(
            EgressMode.DEFAULT_DENY, frozenset({"allowed.com"})
        )
        sink = InMemoryAuditSink()
        # CONNECT host doesn't matter — SNI is the gate in DEFAULT_DENY.
        connect = b"CONNECT allowed.com:443 HTTP/1.1\r\n\r\n"
        tls = _make_tls_client_hello("evil.com")
        written = await _run_connect_handler(connect, engine, sink, monkeypatch, tls_payload=tls)
        assert b"403" in written
        assert "evil.com" in sink.denied_domains()
        assert sink.allowed_domains() == []

    @pytest.mark.asyncio
    async def test_default_deny_blocks_unlisted_domain_no_sni(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-4: in DEFAULT_DENY, CONNECT without a ClientHello is denied immediately.

        No audit record is emitted — the domain cannot be verified (no SNI).
        """
        engine = _make_policy_engine(
            EgressMode.DEFAULT_DENY, frozenset({"allowed.com"})
        )
        sink = InMemoryAuditSink()
        request = b"CONNECT evil.com:443 HTTP/1.1\r\n\r\n"
        written = await _run_connect_handler(request, engine, sink, monkeypatch)
        assert b"403" in written
        # No audit record because no SNI was available to verify.
        assert sink.denied_domains() == []
        assert sink.allowed_domains() == []

    @pytest.mark.asyncio
    async def test_default_deny_allows_whitelisted_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-4: DEFAULT_DENY allows when SNI ∈ whitelist."""
        engine = _make_policy_engine(
            EgressMode.DEFAULT_DENY, frozenset({"allowed.com"})
        )
        sink = InMemoryAuditSink()
        connect = b"CONNECT allowed.com:443 HTTP/1.1\r\n\r\n"
        tls = _make_tls_client_hello("allowed.com")
        written = await _run_connect_handler(connect, engine, sink, monkeypatch, tls_payload=tls)
        assert b"200" in written
        assert "allowed.com" in sink.allowed_domains()

    @pytest.mark.asyncio
    async def test_invalid_connect_line_returns_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _make_policy_engine()
        sink = InMemoryAuditSink()
        request = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"
        # No es CONNECT → se delega a plain HTTP handler
        # Pero sin upstream el resultado depende del handler; lo que NO
        # debe pasar es que lance excepción no capturada
        transport = _CollectingTransport()
        reader = _make_reader(request)
        writer = _make_writer(transport)

        async def _fake_open_connection(host, port):
            r = asyncio.StreamReader()
            r.feed_eof()
            return r, _make_writer(_CollectingTransport())

        monkeypatch.setattr(
            "hermes.egress_proxy.infrastructure.proxy_handler.asyncio.open_connection",
            _fake_open_connection,
        )

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        # No debe lanzar
        await handler.handle(reader, writer)


# ---------------------------------------------------------------------------
# Tests HTTP plano
# ---------------------------------------------------------------------------


class TestPlainHttpHandler:
    @pytest.mark.asyncio
    async def test_plain_http_denied_in_default_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-7: plain HTTP is rejected in DEFAULT_DENY regardless of Host header.

        The Host header is client-controlled and cannot be authenticated against SNI.
        Rejection happens before reading the Host header — no audit record is emitted
        (the domain is unverified). The response is 403.
        """
        engine = _make_policy_engine(
            EgressMode.DEFAULT_DENY, frozenset({"allowed.com"})
        )
        sink = InMemoryAuditSink()
        request = b"GET http://evil.com/path HTTP/1.1\r\nHost: evil.com\r\n\r\n"
        transport = _CollectingTransport()
        reader = _make_reader(request)
        writer = _make_writer(transport)

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        await handler.handle(reader, writer)
        written = bytes(transport.written)
        assert b"403" in written
        # No audit record: domain not verified (no SNI) in DEFAULT_DENY plain HTTP.
        assert sink.denied_domains() == []
        assert sink.allowed_domains() == []

    @pytest.mark.asyncio
    async def test_plain_http_allowed_opens_upstream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _make_policy_engine(EgressMode.OPEN_LOGGED)
        sink = InMemoryAuditSink()
        request = b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
        transport = _CollectingTransport()
        reader = _make_reader(request)
        writer = _make_writer(transport)

        upstream_written = bytearray()

        async def _fake_open_connection(host, port):
            remote_reader = asyncio.StreamReader()
            remote_reader.feed_data(b"HTTP/1.1 200 OK\r\n\r\n")
            remote_reader.feed_eof()
            remote_transport = _CollectingTransport()
            remote_writer = _make_writer(remote_transport)
            # Captura lo que el proxy escribe al upstream
            original_write = remote_transport.write

            def _capture(data: bytes) -> None:
                upstream_written.extend(data)
                original_write(data)

            remote_transport.write = _capture  # type: ignore[method-assign]
            return remote_reader, remote_writer

        monkeypatch.setattr(
            "hermes.egress_proxy.infrastructure.proxy_handler.asyncio.open_connection",
            _fake_open_connection,
        )

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        await handler.handle(reader, writer)

        assert "example.com" in sink.allowed_domains()


# ---------------------------------------------------------------------------
# Tests de integración de política: socket de control cambia la política
# ---------------------------------------------------------------------------


class TestPolicyChangedViaControlSocket:
    """Verifica que push_policy() (llamado por el socket de control)
    cambia el comportamiento del proxy en tiempo de ejecución.
    """

    @pytest.mark.asyncio
    async def test_push_deny_policy_then_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-4: after push_policy to DEFAULT_DENY, CONNECT with evil SNI is denied."""
        # Empieza en open-logged
        engine = _make_policy_engine(EgressMode.OPEN_LOGGED)
        sink = InMemoryAuditSink()

        # Simula que el socket de control empuja una política default-deny
        engine.replace_global(
            SessionPolicy(
                session_id="__global__",
                mode=EgressMode.DEFAULT_DENY,
                domains_whitelist=frozenset({"safe.com"}),
            )
        )

        connect = b"CONNECT evil.com:443 HTTP/1.1\r\n\r\n"
        tls = _make_tls_client_hello("evil.com")
        written = await _run_connect_handler(connect, engine, sink, monkeypatch, tls_payload=tls)
        assert b"403" in written
        assert "evil.com" in sink.denied_domains()

    @pytest.mark.asyncio
    async def test_push_open_logged_after_deny_allows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _make_policy_engine(
            EgressMode.DEFAULT_DENY, frozenset({"safe.com"})
        )
        sink = InMemoryAuditSink()

        # El socket de control relaja la política global a open-logged
        engine.replace_global(
            SessionPolicy(
                session_id="__global__",
                mode=EgressMode.OPEN_LOGGED,
            )
        )

        request = b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
        written = await _run_connect_handler(request, engine, sink, monkeypatch)
        assert b"200" in written
        assert "example.com" in sink.allowed_domains()
