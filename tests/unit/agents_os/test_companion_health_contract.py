"""A reachable companion is not necessarily compatible or ready."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from hermes.agents_os.infrastructure.companion_health_check import CompanionHealthChecker

pytestmark = pytest.mark.unit

HEALTH = {
    "status": "ok",
    "contract_version": "1.0.0",
    "accounts_linked": {"google": True, "meta": False},
    "db": "ok",
}


class Response:
    status = 200

    def __init__(self, body=HEALTH, *, raw=None, encoding="identity"):
        self.body = deepcopy(body)
        self.raw = raw if raw is not None else json.dumps(body).encode()
        self.headers = {"Content-Encoding": encoding}
        self.content = self
        self.read_count = 0

    async def json(self, **_kwargs):
        return self.body

    async def iter_chunked(self, size):
        for offset in range(0, len(self.raw), size):
            self.read_count += len(self.raw[offset : offset + size])
            yield self.raw[offset : offset + size]


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", "2.0.0"),
        ("contract_version", "1.1.0"),
        ("contract_version", "1.0.0+unreviewed"),
        ("contract_version", 1),
        ("contract_version", None),
        ("status", "degraded"),
        ("status", None),
        ("db", "error"),
        ("db", None),
        ("accounts_linked", {"google": "false", "meta": False}),
        ("accounts_linked", {"google": 1, "meta": False}),
        ("accounts_linked", {"google": [], "meta": False}),
        ("accounts_linked", {"google": True}),
        ("accounts_linked", {"google": True, "meta": False, "token": "do-not-echo"}),
        ("accounts_linked", None),
    ],
)
async def test_incompatible_or_unhealthy_never_becomes_ready(field, value):
    body = deepcopy(HEALTH)
    body[field] = value
    report = await CompanionHealthChecker()._interpret("safent-ads", Response(body))
    assert report.state == "unreachable"
    assert report.reachable is True
    assert report.accounts_linked is None
    assert "do-not-echo" not in repr(report)


@pytest.mark.parametrize("body", [None, [], "text", {}, {**HEALTH, "extra": "do-not-echo"}])
async def test_malformed_shape_is_not_no_accounts(body):
    report = await CompanionHealthChecker()._interpret("safent-ads", Response(body))
    assert report.state == "unreachable"
    assert report.accounts_linked is None


async def test_unknown_slug_has_no_implicit_contract():
    report = await CompanionHealthChecker()._interpret("another-companion", Response())
    assert report.state == "unreachable"


async def test_response_body_is_bounded_before_json_and_never_echoed():
    response = Response(raw=b"do-not-echo" * 100_000)
    report = await CompanionHealthChecker()._interpret("safent-ads", response)
    assert report.state == "unreachable"
    assert response.read_count <= 10 * 1024
    assert "do-not-echo" not in repr(report)


@pytest.mark.parametrize(
    "raw", [b"not-json", b"\xff", b"[" * 7000], ids=["invalid", "utf8", "depth"]
)
async def test_invalid_json_utf8_and_deep_json_are_fail_closed(raw):
    report = await CompanionHealthChecker()._interpret("safent-ads", Response(raw=raw))
    assert report.state == "unreachable"


async def test_compressed_body_rejected_before_read():
    response = Response(encoding="gzip")
    report = await CompanionHealthChecker()._interpret("safent-ads", response)
    assert report.state == "unreachable"
    assert response.read_count == 0


async def test_healthy_then_changed_companion_does_not_reuse_ready():
    checker = CompanionHealthChecker()
    first = await checker._interpret("safent-ads", Response())
    changed = await checker._interpret(
        "safent-ads", Response({**HEALTH, "contract_version": "2.0.0"})
    )
    restored = await checker._interpret("safent-ads", Response())
    assert (first.state, changed.state, restored.state) == ("ready", "unreachable", "ready")


@pytest.mark.parametrize("redirect", [False, True])
async def test_real_tls_probe_uses_private_destination_and_never_follows_redirect(
    tmp_path, redirect
):
    import asyncio
    import ssl
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ads.safent.internal")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("ads.safent.internal")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(cert_path, key_path)
    requests = []

    async def handler(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            requests.append(request)
            if redirect and b"GET /mcp/health " in request:
                response = (
                    b"HTTP/1.1 302 Found\r\nLocation: /other\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                body = json.dumps(HEALTH).encode()
                response = (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ssl)
    async with server:
        endpoint = SimpleNamespace(
            host="ads.safent.internal", ip="127.0.0.1", port=server.sockets[0].getsockname()[1]
        )
        report = await CompanionHealthChecker()._fetch(
            "safent-ads",
            endpoint=endpoint,
            bearer="fake-health-token",
            ssl_ctx=ssl.create_default_context(cafile=str(cert_path)),
        )
    assert report.state == ("unreachable" if redirect else "ready")
    assert report.http_status == (302 if redirect else 200)
    assert len(requests) == 1
    assert b"Authorization: Bearer fake-health-token" in requests[0]
    assert "fake-health-token" not in repr(report)
