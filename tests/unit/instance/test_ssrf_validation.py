"""SSRF / https-only validation tests for HttpControlPlaneClient and PairRequest.

Tests verify that blocked endpoints are rejected before any network call:
  - http:// scheme → PairingError
  - file:// scheme → PairingError
  - https://localhost → PairingError
  - https://127.0.0.1 → PairingError
  - https://169.254.169.254 → PairingError (metadata endpoint)
  - https://10.0.0.1 → PairingError (RFC 1918)
  - https://192.168.1.1 → PairingError (RFC 1918)
  - https://172.16.0.1 → PairingError (RFC 1918)
  - https://[::1] → PairingError (IPv6 loopback)
  - https://cloud.safent.run → allowed (no exception at construction time)
"""

from __future__ import annotations

import pytest

from hermes.instance.infrastructure.http_control_plane_client import (
    HttpControlPlaneClient,
    _validate_cloud_endpoint,
)
from hermes.instance.pairing_service import PairingError

pytestmark = pytest.mark.unit


class TestHttpSchemeValidation:
    def test_http_scheme_rejected(self) -> None:
        with pytest.raises(PairingError, match="https://"):
            _validate_cloud_endpoint("http://cloud.safent.run")

    def test_file_scheme_rejected(self) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint("file:///etc/passwd")

    def test_no_scheme_rejected(self) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint("cloud.safent.run")

    def test_https_accepted(self) -> None:
        # Must not raise.
        _validate_cloud_endpoint("https://cloud.safent.run")


class TestSsrfBlockedHosts:
    @pytest.mark.parametrize("url", [
        "https://localhost",
        "https://localhost:8080",
        "https://metadata.google.internal",
    ])
    def test_blocked_hostname_rejected(self, url: str) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint(url)

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1",
        "https://127.0.0.1:7517",
        "https://127.1.2.3",
    ])
    def test_loopback_ipv4_rejected(self, url: str) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint(url)

    def test_loopback_ipv6_rejected(self) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint("https://[::1]")

    def test_metadata_endpoint_rejected(self) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint("https://169.254.169.254")

    def test_link_local_rejected(self) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint("https://169.254.0.1")

    @pytest.mark.parametrize("url", [
        "https://10.0.0.1",
        "https://10.255.255.255",
        "https://172.16.0.1",
        "https://172.31.255.254",
        "https://192.168.0.1",
        "https://192.168.255.255",
    ])
    def test_rfc1918_rejected(self, url: str) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint(url)

    def test_wildcard_zero_rejected(self) -> None:
        with pytest.raises(PairingError):
            _validate_cloud_endpoint("https://0.0.0.0")


class TestSsrfAllowedHosts:
    @pytest.mark.parametrize("url", [
        "https://cloud.safent.run",
        "https://enterprise.example.com",
        "https://enterprise.example.com:443",
        "https://my-company.safent.io/pairing",
    ])
    def test_safe_url_accepted(self, url: str) -> None:
        _validate_cloud_endpoint(url)


class TestHttpControlPlaneClientConstruction:
    def test_http_raises_at_construction(self) -> None:
        with pytest.raises(PairingError):
            HttpControlPlaneClient(cloud_endpoint="http://evil.example.com")

    def test_loopback_raises_at_construction(self) -> None:
        with pytest.raises(PairingError):
            HttpControlPlaneClient(cloud_endpoint="https://127.0.0.1:7517")

    def test_safe_endpoint_constructs(self) -> None:
        client = HttpControlPlaneClient(cloud_endpoint="https://cloud.safent.run")
        assert client is not None
