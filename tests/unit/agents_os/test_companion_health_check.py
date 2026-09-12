"""CompanionHealthChecker (026, T004) — daemon-side `/mcp/health` reader.

Covers: fail-soft to `not_installed` when the companion isn't provisioned or
has no bearer (never an exception — a companion is optional infrastructure,
FR-3), the shared `FixedIpResolver` pins DNS to the validated companion IP without
touching the request hostname (SNI/cert verification stays intact), and the
response-interpretation logic (`_interpret`) derives the honest FR-003/FR-009
states from the companion's real `/mcp/health` payload shape
(`{status, contract_version, accounts_linked: {google, meta}, db}`) —
never a fabricated "ready".
"""

from __future__ import annotations

import json
import socket

import pytest

from hermes.agents_os.infrastructure.companion_health_check import CompanionHealthChecker
from hermes.shell_server import companions as companions_mod
from hermes.shell_server.companion_net import FixedIpResolver

pytestmark = pytest.mark.unit

_SLUG = "safent-ads"


class _FakeEndpoint:
    def __init__(self) -> None:
        self.host = "ads.safent.internal"
        self.ip = "10.201.0.10"
        self.port = 8443
        self.ca_path = "/etc/hermes/companions/ads-ca.crt"


class _FakeJsonResponse:
    def __init__(self, *, status: int, body: object) -> None:
        self.status = status
        self._body = body
        self.headers = {}
        self.content = self

    async def iter_chunked(self, size: int):
        data = json.dumps(self._body).encode()
        for offset in range(0, len(data), size):
            yield data[offset : offset + size]

    async def json(self, *, content_type: str | None = None) -> object:  # noqa: ARG002
        return self._body


# ============================================================================
# Fail-soft when the companion isn't reachable at the config layer
# ============================================================================


class TestNotInstalled:
    async def test_no_companion_entry_reports_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: None)
        checker = CompanionHealthChecker()

        report = await checker.check(_SLUG)

        assert report.state == "not_installed"
        assert report.reachable is False

    async def test_no_bearer_reports_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: _FakeEndpoint())
        monkeypatch.setattr(companions_mod, "read_companion_bearer", lambda _ep: None)
        checker = CompanionHealthChecker()

        report = await checker.check(_SLUG)

        assert report.state == "not_installed"
        assert report.reachable is False

    async def test_unreadable_ca_reports_unreachable_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoint = _FakeEndpoint()
        endpoint.ca_path = "/does/not/exist.crt"
        monkeypatch.setattr(companions_mod, "get_companion", lambda _slug: endpoint)
        monkeypatch.setattr(companions_mod, "read_companion_bearer", lambda _ep: "bearer-value")
        checker = CompanionHealthChecker()

        report = await checker.check(_SLUG)

        assert report.state == "unreachable"
        assert report.reachable is False


# ============================================================================
# FixedIpResolver (shell_server.companion_net) — DNS pinned to the validated IP
# ============================================================================


class TestFixedIpResolver:
    async def test_resolves_the_pinned_hostname_to_the_configured_ip(self) -> None:
        resolver = FixedIpResolver(hostname="ads.safent.internal", ip="10.201.0.10")

        results = await resolver.resolve("ads.safent.internal", port=8443)

        assert len(results) == 1
        assert results[0]["host"] == "10.201.0.10"
        assert results[0]["hostname"] == "ads.safent.internal"
        assert results[0]["port"] == 8443
        assert results[0]["family"] == socket.AF_INET

    async def test_a_different_host_is_passed_through_unpinned(self) -> None:
        resolver = FixedIpResolver(hostname="ads.safent.internal", ip="10.201.0.10")

        results = await resolver.resolve("some.other.host", port=443)

        assert results[0]["host"] == "some.other.host"

    async def test_close_is_a_no_op(self) -> None:
        resolver = FixedIpResolver(hostname="ads.safent.internal", ip="10.201.0.10")
        await resolver.close()  # must not raise


# ============================================================================
# Response interpretation — honest states from the real /mcp/health shape
# ============================================================================


class TestInterpretResponse:
    async def test_401_reports_unauthorized(self) -> None:
        checker = CompanionHealthChecker()
        response = _FakeJsonResponse(status=401, body={})

        report = await checker._interpret(_SLUG, response)  # noqa: SLF001

        assert report.state == "unauthorized"
        assert report.reachable is True
        assert report.http_status == 401

    async def test_non_200_non_401_reports_unreachable(self) -> None:
        checker = CompanionHealthChecker()
        response = _FakeJsonResponse(status=503, body={})

        report = await checker._interpret(_SLUG, response)  # noqa: SLF001

        assert report.state == "unreachable"
        assert report.http_status == 503

    async def test_ok_with_no_linked_accounts_reports_no_accounts(self) -> None:
        checker = CompanionHealthChecker()
        body = {
            "status": "ok",
            "contract_version": "1.0.0",
            "accounts_linked": {"google": False, "meta": False},
            "db": "ok",
        }
        response = _FakeJsonResponse(status=200, body=body)

        report = await checker._interpret(_SLUG, response)  # noqa: SLF001

        assert report.state == "no_accounts"
        assert report.reachable is True
        assert report.contract_version == "1.0.0"
        assert report.accounts_linked == {"google": False, "meta": False}

    async def test_ok_with_at_least_one_linked_account_reports_ready(self) -> None:
        checker = CompanionHealthChecker()
        body = {
            "status": "ok",
            "contract_version": "1.0.0",
            "accounts_linked": {"google": True, "meta": False},
            "db": "ok",
        }
        response = _FakeJsonResponse(status=200, body=body)

        report = await checker._interpret(_SLUG, response)  # noqa: SLF001

        assert report.state == "ready"

    async def test_malformed_body_reports_unreachable_not_an_exception(self) -> None:
        checker = CompanionHealthChecker()
        response = _FakeJsonResponse(status=200, body="not-a-dict")

        report = await checker._interpret(_SLUG, response)  # noqa: SLF001

        assert report.state == "unreachable"  # malformed does not mean valid-but-unconfigured
        assert report.reachable is True
