"""Tests del parser de Host header y línea CONNECT.

Cubre:
  - parse_connect_line: happy path, sin puerto, IPv6, error.
  - parse_host_header: happy path, con puerto, vacío, error.
"""

from __future__ import annotations

import pytest

from hermes.egress_proxy.domain.host_parser import (
    HostParseError,
    parse_connect_line,
    parse_host_header,
)

pytestmark = pytest.mark.unit


class TestParseConnectLine:
    def test_basic_connect(self) -> None:
        t = parse_connect_line("CONNECT example.com:443 HTTP/1.1")
        assert t.host == "example.com"
        assert t.port == 443

    def test_lowercase_connect(self) -> None:
        t = parse_connect_line("connect Example.COM:8443 HTTP/1.1")
        assert t.host == "example.com"
        assert t.port == 8443

    def test_default_port_443(self) -> None:
        # host sin puerto en CONNECT es inválido según RFC, pero el parser
        # debe ser robusto y asignar 443 por defecto
        t = parse_connect_line("CONNECT example.com HTTP/1.1")
        assert t.host == "example.com"
        assert t.port == 443

    def test_ipv6_connect(self) -> None:
        t = parse_connect_line("CONNECT [::1]:8080 HTTP/1.1")
        assert t.host == "::1"
        assert t.port == 8080

    def test_not_connect_raises(self) -> None:
        with pytest.raises(HostParseError, match="CONNECT"):
            parse_connect_line("GET http://example.com/ HTTP/1.1")

    def test_empty_raises(self) -> None:
        with pytest.raises(HostParseError):
            parse_connect_line("")

    def test_invalid_port_raises(self) -> None:
        with pytest.raises(HostParseError, match="numérico"):
            parse_connect_line("CONNECT example.com:abc HTTP/1.1")

    def test_port_out_of_range_raises(self) -> None:
        with pytest.raises(HostParseError, match="rango"):
            parse_connect_line("CONNECT example.com:99999 HTTP/1.1")


class TestParseHostHeader:
    def test_basic_host(self) -> None:
        h = parse_host_header("example.com")
        assert h.host == "example.com"
        assert h.port == 80

    def test_host_with_port(self) -> None:
        h = parse_host_header("example.com:8080")
        assert h.host == "example.com"
        assert h.port == 8080

    def test_uppercase_normalized(self) -> None:
        h = parse_host_header("EXAMPLE.COM")
        assert h.host == "example.com"

    def test_trailing_dot_stripped(self) -> None:
        h = parse_host_header("example.com.")
        assert h.host == "example.com"

    def test_empty_raises(self) -> None:
        with pytest.raises(HostParseError, match="vacío"):
            parse_host_header("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(HostParseError, match="vacío"):
            parse_host_header("   ")

    def test_ipv6_host(self) -> None:
        h = parse_host_header("[::1]:9000")
        assert h.host == "::1"
        assert h.port == 9000
