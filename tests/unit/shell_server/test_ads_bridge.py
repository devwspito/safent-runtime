"""ads_bridge (026, contracts/sso.md) — the same-origin reverse proxy from
Safent to the safent-ads companion.

Runs the REAL router against a REAL local TLS companion double (self-signed
CA + leaf for `ads.safent.internal`, `FixedIpResolver` pins the connection
to 127.0.0.1) — only the D-Bus mint call is mocked (T004 covers minting in
isolation, tests/unit/agents_os/test_companion_sso_assertion.py).

Uses `httpx.AsyncClient(transport=ASGITransport(...))` rather than the sync
`fastapi.testclient.TestClient`: the fake companion is a real asyncio TCP
server bound to the SAME event loop the test runs in (async fixture); the
sync TestClient drives the ASGI app from a background thread with its OWN
loop, which never returns control to the fixture's loop while a request is
in flight — the fake server would accept the TCP connection but nothing
would ever drive its event loop, so every request would time out.

Covers the sso.md §7 threat table entries this proxy owns:
  S-2  — every exchange mints a FRESH assertion (never replays a cached one)
  E-1  — /ads/mcp* denied (403 MCP_NOT_BRIDGED)
  E-2  — upstream security headers (CSP, X-Frame-Options) pass through unmodified
  E-3  — only the ads_csrf cookie + x-csrf-token header travel upstream
  I-1  — Authorization/original Cookie/X-Forwarded-For never reach the companion
  I-2  — ads_session never reaches the browser (retained in the in-process jar)
  T-1  — a client-supplied X-Forwarded-Prefix is ignored; always "/ads"
  T-1b — X-Forwarded-Host/Proto are the browser origin; client values overwritten
  T-2  — /ads/api/v1/auth/{login,totp} denied (403 LOGIN_NOT_BRIDGED)
  E-4  — no ads_bridge cookie -> 401 on every path, allowed or not
plus the 504 when the companion is unreachable/times out.
"""

from __future__ import annotations

import asyncio
import ssl as ssl_mod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hermes.shell_server import ads_bridge as bridge_mod
from hermes.shell_server import companions as companions_mod
from hermes.shell_server.ads_bridge import AdsSessionJar, create_ads_bridge_router
from hermes.shell_server.security import secrets as secrets_mod

pytestmark = pytest.mark.unit

_HOSTNAME = "ads.safent.internal"


# ============================================================================
# Fake companion — real TLS server on 127.0.0.1, cert issued for _HOSTNAME
# ============================================================================


def _make_ca_and_leaf(tmp_path: Path) -> tuple[Path, Path, Path]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test ads CA")])
    now = datetime.now(UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _HOSTNAME)])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_HOSTNAME)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    leaf_crt_path = tmp_path / "leaf.crt"
    leaf_crt_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    leaf_key_path = tmp_path / "leaf.key"
    leaf_key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, leaf_crt_path, leaf_key_path


class _FakeEndpoint:
    def __init__(self, *, ip: str, port: int, ca_path: str) -> None:
        self.host = _HOSTNAME
        self.ip = ip
        self.port = port
        self.ca_path = ca_path


class _FakeCompanion:
    """Records what it received so tests can assert on it directly."""

    def __init__(self) -> None:
        self.exchange_calls: list[dict] = []
        self.logout_calls: list[dict] = []
        self.probe_calls: list[dict] = []
        self.exchange_should_fail = False
        self.probe_status = 200

    async def exchange(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.exchange_calls.append(body)
        if self.exchange_should_fail:
            return web.json_response({"error": {"code": "ASSERTION_INVALID"}}, status=401)
        resp = web.json_response({"owner_id": "o1", "business_id": "b1"})
        resp.set_cookie(
            "ads_session", f"session-{len(self.exchange_calls)}",
            httponly=True, secure=True, samesite="Strict", path="/api", max_age=60,
        )
        return resp

    async def logout(self, request: web.Request) -> web.Response:
        self.logout_calls.append(dict(request.cookies))
        return web.json_response({"status": "logged_out"})

    async def probe(self, request: web.Request) -> web.Response:
        self.probe_calls.append(
            {
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "cookies": dict(request.cookies),
                "method": request.method,
                "body": await request.read(),
                "query": request.query_string,
            }
        )
        if request.cookies.get("ads_session") == "stale-session":
            return web.json_response({"error": "session expired"}, status=401)
        if self.probe_status != 200:
            return web.json_response({"error": "boom"}, status=self.probe_status)
        resp = web.json_response({"ok": True})
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.set_cookie("ads_csrf", "csrf-from-companion", path="/api", samesite="Strict")
        return resp

    async def root(self, request: web.Request) -> web.Response:  # noqa: ARG002
        return web.Response(text="<html>root</html>", content_type="text/html")


@pytest.fixture()
async def fake_companion(tmp_path: Path):
    ca_path, leaf_crt, leaf_key = _make_ca_and_leaf(tmp_path)
    companion = _FakeCompanion()
    app = web.Application()
    app.router.add_post("/api/v1/auth/exchange", companion.exchange)
    app.router.add_post("/api/v1/auth/logout", companion.logout)
    app.router.add_route("*", "/api/v1/probe", companion.probe)
    app.router.add_get("/", companion.root)

    ssl_ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(str(leaf_crt), str(leaf_key))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_ctx)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    endpoint = _FakeEndpoint(ip="127.0.0.1", port=port, ca_path=str(ca_path))
    yield companion, endpoint
    await runner.cleanup()


# ============================================================================
# App / client wiring
# ============================================================================


@pytest.fixture(autouse=True)
def master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\x33" * 32)
    monkeypatch.setattr(secrets_mod, "_MASTER_KEY_PATH", key_file)


def _mint_proxy(*, assertion: str = "fresh-assertion") -> MagicMock:
    proxy = MagicMock()
    proxy.call_dict = AsyncMock(
        side_effect=lambda member, *_a: {
            "mint_companion_owner_assertion": {"assertion": assertion, "expires_at": "x"},
            "get_companion_health": {"state": "ready"},
        }[member]
    )
    return proxy


class _AppClient:
    """Bundles the FastAPI app + an ASGI-transport AsyncClient — tests need
    direct access to `app.state.ads_session_jar` in a couple of cases."""

    def __init__(self, app: FastAPI, client: AsyncClient) -> None:
        self.app = app
        self.client = client


def _make_client(*, endpoint, proxy: MagicMock) -> _AppClient:  # noqa: ARG001
    app = FastAPI()
    app.state.dbus_proxy = proxy
    app.state.ads_session_jar = AdsSessionJar()
    app.include_router(create_ads_bridge_router())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return _AppClient(app, client)


def _valid_bridge_cookie() -> str:
    return bridge_mod._bridge_cookie_value()  # noqa: SLF001


def _patch_companion(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: endpoint)


# ============================================================================
# POST/DELETE /api/v1/ads/bridge/session
# ============================================================================


class TestMintBridgeSession:
    async def test_issues_the_stable_bridge_cookie(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            r = await client.post("/api/v1/ads/bridge/session")

            assert r.status_code == 200
            assert r.json()["status"] == "ready"
            assert r.cookies.get("ads_bridge") == _valid_bridge_cookie()

    async def test_reports_unavailable_when_health_is_not_ready(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        proxy = MagicMock()
        proxy.call_dict = AsyncMock(return_value={"state": "no_accounts"})
        ac = _make_client(endpoint=endpoint, proxy=proxy)

        async with ac.client as client:
            r = await client.post("/api/v1/ads/bridge/session")

            assert r.status_code == 200
            assert r.json() == {"status": "unavailable", "reason": "no_accounts"}
            # cookie is still issued — reading works even before accounts are linked
            assert r.cookies.get("ads_bridge") == _valid_bridge_cookie()

    async def test_delete_clears_the_cookie_and_logs_out_upstream(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            # populate the jar first via a proxied call
            r = await client.get("/ads/api/v1/probe")
            assert r.status_code == 200

            r = await client.delete("/api/v1/ads/bridge/session")

            assert r.status_code == 200
            assert len(companion.logout_calls) == 1


# ============================================================================
# E-4 — no ads_bridge cookie -> 401 on every path
# ============================================================================


class TestBridgeCookieRequired:
    @pytest.mark.parametrize("path", ["/ads", "/ads/", "/ads/api/v1/probe", "/ads/assets/x.js"])
    async def test_missing_cookie_denied(self, fake_companion, path: str) -> None:
        _companion, endpoint = fake_companion
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            r = await client.get(path)

            assert r.status_code == 401
            assert r.json()["error"]["code"] == "BRIDGE_COOKIE_REQUIRED"

    async def test_wrong_cookie_value_denied(self, fake_companion) -> None:
        _companion, endpoint = fake_companion
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", "0" * 64)
            r = await client.get("/ads/api/v1/probe")

            assert r.status_code == 401


# ============================================================================
# E-1 / T-2 — denied prefixes
# ============================================================================


class TestDeniedPrefixes:
    @pytest.mark.parametrize("path", ["/ads/mcp", "/ads/mcp/tools"])
    async def test_mcp_prefix_denied(self, fake_companion, path: str) -> None:
        _companion, endpoint = fake_companion
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get(path)

            assert r.status_code == 403
            assert r.json()["error"]["code"] == "MCP_NOT_BRIDGED"

    @pytest.mark.parametrize(
        "path", ["/ads/api/v1/auth/login", "/ads/api/v1/auth/totp"]
    )
    async def test_login_routes_denied(self, fake_companion, path: str) -> None:
        _companion, endpoint = fake_companion
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get(path)

            assert r.status_code == 403
            assert r.json()["error"]["code"] == "LOGIN_NOT_BRIDGED"

    async def test_unmapped_path_denied(self, fake_companion) -> None:
        _companion, endpoint = fake_companion
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/something-not-allowlisted")

            assert r.status_code == 403


# ============================================================================
# I-1 / I-2 / E-3 / T-1 — credential handling on a successful proxied call
# ============================================================================


class TestProxiedRequestCredentialHandling:
    async def test_ads_session_never_reaches_the_browser(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.status_code == 200
            assert "ads_session" not in r.cookies
            set_cookie_headers = r.headers.get_list("set-cookie")
            assert not any("ads_session" in h for h in set_cookie_headers)

    async def test_ads_csrf_cookie_is_rewritten_and_forwarded(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.cookies.get("ads_csrf") == "csrf-from-companion"

    async def test_security_headers_forwarded_unmodified(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
            assert r.headers["x-frame-options"] == "SAMEORIGIN"

    async def test_authorization_and_original_cookies_are_stripped(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            client.cookies.set("shell-webui-session", "top-secret-safent-bearer")

            r = await client.get(
                "/ads/api/v1/probe",
                headers={
                    "Authorization": "Bearer top-secret",
                    "X-Forwarded-For": "1.2.3.4",
                },
            )

            assert r.status_code == 200
            received = companion.probe_calls[-1]
            assert "authorization" not in received["headers"]
            assert "x-forwarded-for" not in received["headers"]
            assert "shell-webui-session" not in received["cookies"]
            assert "ads_bridge" not in received["cookies"]

    async def test_csrf_pair_is_forwarded_cookie_and_header(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            client.cookies.set("ads_csrf", "browser-csrf-value")

            r = await client.post(
                "/ads/api/v1/probe", headers={"X-Csrf-Token": "browser-csrf-value"}
            )

            assert r.status_code == 200
            received = companion.probe_calls[-1]
            assert received["cookies"]["ads_csrf"] == "browser-csrf-value"
            assert received["headers"]["x-csrf-token"] == "browser-csrf-value"

    async def test_owner_confirmation_preserves_exact_request_without_legacy_otp(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())
        body = b'{"amount": "10.00", "platform_account_id": "fixture-account"}'
        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            client.cookies.set("ads_csrf", "fixture-csrf")
            response = await client.post(
                "/ads/api/v1/probe?review=explicit",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Csrf-Token": "fixture-csrf",
                    "X-Action-Confirmation": "fixture-one-shot-proof",
                    "X-Reauth-Token": "retired-otp-header",
                },
            )
        assert response.status_code == 200
        received = companion.probe_calls[-1]
        assert received["headers"]["x-action-confirmation"] == "fixture-one-shot-proof"
        assert "x-reauth-token" not in received["headers"]
        assert received["headers"]["x-csrf-token"] == "fixture-csrf"
        assert received["cookies"]["ads_csrf"] == "fixture-csrf"
        assert received["method"] == "POST"
        assert received["body"] == body
        assert received["query"] == "review=explicit"
        assert len(companion.probe_calls) == 1

    async def test_client_supplied_forwarded_prefix_is_ignored(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get(
                "/ads/api/v1/probe", headers={"X-Forwarded-Prefix": "/evil"}
            )

            assert r.status_code == 200
            assert companion.probe_calls[-1]["headers"]["x-forwarded-prefix"] == "/ads"

    async def test_forwarded_host_and_proto_are_the_browser_origin_not_client_supplied(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-1b: the companion derives OAuth redirect URIs from these two headers,
        so they must be the origin the browser used and never a client value."""
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get(
                "/ads/api/v1/probe",
                headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "ftp"},
            )

            assert r.status_code == 200
            sent = companion.probe_calls[-1]["headers"]
            # the test client's origin is http://test — that, not the client's header
            assert sent["x-forwarded-host"] == "test"
            assert sent["x-forwarded-proto"] == "http"

    async def test_session_cookie_carries_the_jar_value(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            await client.get("/ads/api/v1/probe")

            assert companion.probe_calls[-1]["cookies"]["ads_session"] == "session-1"


# ============================================================================
# S-2 — every exchange mints a fresh assertion
# ============================================================================


class TestFreshAssertionPerExchange:
    async def test_a_forced_reexchange_mints_a_new_assertion(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        proxy = _mint_proxy()
        ac = _make_client(endpoint=endpoint, proxy=proxy)

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())

            await client.get("/ads/api/v1/probe")  # first exchange
            # force a second exchange: the companion now rejects the session
            companion.probe_status = 401
            await client.get("/ads/api/v1/probe")

            assert proxy.call_dict.await_count >= 2
            mint_calls = [
                c for c in proxy.call_dict.await_args_list
                if c.args[0] == "mint_companion_owner_assertion"
            ]
            assert len(mint_calls) >= 2
            assert len(companion.exchange_calls) >= 2


# ============================================================================
# Lazy exchange with one retry on a stale session
# ============================================================================


class TestOneRetryOnUpstream401:
    async def test_confirmed_mutation_never_replays_under_a_new_session(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())
        ac.app.state.ads_session_jar.set(cookie="stale-session", ttl_seconds=3600)
        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            response = await client.post(
                "/ads/api/v1/probe",
                content=b'{"intent":"fixture"}',
                headers={"X-Action-Confirmation": "fixture-one-shot-proof"},
            )
        assert response.status_code == 401
        assert len(companion.probe_calls) == 1
        assert companion.exchange_calls == []

    async def test_stale_session_triggers_exactly_one_retry(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())
        ac.app.state.ads_session_jar.set(cookie="stale-session", ttl_seconds=3600)

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.status_code == 200
            assert len(companion.exchange_calls) == 1  # exactly one re-exchange
            assert companion.probe_calls[0]["cookies"]["ads_session"] == "stale-session"
            assert companion.probe_calls[1]["cookies"]["ads_session"] == "session-1"

    async def test_exchange_failure_after_401_propagates(
        self, fake_companion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        companion, endpoint = fake_companion
        _patch_companion(monkeypatch, endpoint)
        companion.exchange_should_fail = True
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.status_code == 503
            assert r.json()["error"]["code"] == "ADS_SESSION_UNAVAILABLE"


# ============================================================================
# 504 — companion down
# ============================================================================


class TestCompanionDown:
    async def test_connection_refused_reports_504(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca_path, _leaf, _key = _make_ca_and_leaf(tmp_path)
        # Port 1 is a privileged, virtually-never-bound port — a real,
        # syntactically valid CA so the failure is a genuine connection
        # refusal at the network layer, not an SSL-context construction error.
        endpoint = _FakeEndpoint(ip="127.0.0.1", port=1, ca_path=str(ca_path))
        monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.status_code == 504
            assert r.json()["error"]["code"] == "COMPANION_TIMEOUT"

    async def test_no_companion_provisioned_reports_504(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: None)
        ac = _make_client(endpoint=None, proxy=_mint_proxy())

        async with ac.client as client:
            client.cookies.set("ads_bridge", _valid_bridge_cookie())
            r = await client.get("/ads/api/v1/probe")

            assert r.status_code == 504

    async def test_real_response_timeout_reports_504(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A companion that accepts the TCP connection but never responds —
        exercises the response timeout, not just connection-refused."""
        ca_path, _leaf, _key = _make_ca_and_leaf(tmp_path)

        async def _black_hole(_reader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(2)
            writer.close()

        server = await asyncio.start_server(_black_hole, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(bridge_mod, "_CONNECT_TIMEOUT_S", 0.3)
        monkeypatch.setattr(bridge_mod, "_TOTAL_TIMEOUT_S", 0.3)

        endpoint = _FakeEndpoint(ip="127.0.0.1", port=port, ca_path=str(ca_path))
        monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: endpoint)
        ac = _make_client(endpoint=endpoint, proxy=_mint_proxy())

        try:
            async with ac.client as client:
                client.cookies.set("ads_bridge", _valid_bridge_cookie())
                r = await client.get("/ads/api/v1/probe")
                assert r.status_code == 504
                assert r.json()["error"]["code"] == "COMPANION_TIMEOUT"
        finally:
            server.close()
            await server.wait_closed()
