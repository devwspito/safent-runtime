"""Tests ApiCallSurfaceAdapter — sin red real (mock aiohttp).

FR-027/028 + fail-closed host allowlist + redact secrets.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.api_call_surface_adapter import (
    ApiCallSurfaceAdapter,
)

pytestmark = pytest.mark.unit


class TestHostAllowlist:
    def test_empty_allowlist_rejected(self) -> None:
        with pytest.raises(ValueError, match="fail-closed"):
            ApiCallSurfaceAdapter(allowed_hosts=())

    def test_assert_host_allowed_accepts_exact(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("api.example.com",))
        # No raise
        a._assert_host_allowed("https://api.example.com/v1/items")

    def test_assert_host_allowed_accepts_subdomain(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        a._assert_host_allowed("https://api.example.com/v1")
        a._assert_host_allowed("https://example.com/")

    def test_assert_host_allowed_rejects_outside(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        with pytest.raises(PermissionError, match="allowlist"):
            a._assert_host_allowed("https://evil.com/v1")

    def test_empty_url_rejected(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        with pytest.raises(PermissionError, match="url vacía"):
            a._assert_host_allowed("")


class TestRedactHeaders:
    def test_authorization_redacted(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        out = a._redact_headers({"Authorization": "Bearer SECRET-123"})
        assert "SECRET-123" not in str(out)
        assert "[[REDACTED:Authorization]]" == out["Authorization"]

    def test_cookie_redacted(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        out = a._redact_headers({"Cookie": "session=abc"})
        assert "abc" not in str(out)

    def test_x_api_key_redacted(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        out = a._redact_headers({"X-API-Key": "key-12345"})
        assert "key-12345" not in str(out)

    def test_normal_headers_preserved(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        out = a._redact_headers(
            {"Content-Type": "application/json", "User-Agent": "Hermes/1.0"}
        )
        assert out["Content-Type"] == "application/json"
        assert out["User-Agent"] == "Hermes/1.0"


class TestSerializeForSigning:
    def test_serialize_deterministic_canonical(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        action = CapturedAction(
            surface_kind=SurfaceKind.API_CALL,
            intent_desc="list items",
            payload={
                "method": "GET",
                "url": "https://api.example.com/items",
                "status_expected": 200,
                "headers_redacted": {"Authorization": "[[REDACTED:Authorization]]"},
            },
        )
        sig1 = a.serialize_for_signing(action)
        sig2 = a.serialize_for_signing(action)
        assert sig1 == sig2
        # Headers redactados no van en la firma (varían entre runs).
        assert b"[[REDACTED" not in sig1
        # Method y URL sí van.
        assert b'"method":"GET"' in sig1
        assert b"api.example.com" in sig1


class TestSurfaceMismatch:
    @pytest.mark.asyncio
    async def test_replay_rejects_wrong_surface(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        wrong = CapturedAction(
            surface_kind=SurfaceKind.TERMINAL,
            intent_desc="wrong",
            payload={"method": "GET", "url": "https://example.com/"},
        )
        outcome = await a.replay(wrong)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY


class TestUnsupportedMethod:
    @pytest.mark.asyncio
    async def test_capture_rejects_method(self) -> None:
        a = ApiCallSurfaceAdapter(allowed_hosts=("example.com",))
        with pytest.raises(ValueError, match="no soportado"):
            await a.capture(
                intent_desc="weird",
                params={"method": "TRACE", "url": "https://example.com/"},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )
