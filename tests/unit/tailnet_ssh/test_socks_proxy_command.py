"""socks_proxy_command — pure SOCKS5 framing (handshake + connect-request
byte layout, and the connect-reply length arithmetic). No sockets here — the
real handshake-over-a-socket path is covered by the fake-SOCKS5-listener
integration test."""

from __future__ import annotations

import pytest

from hermes.tailnet_ssh.infrastructure.socks_proxy_command import (
    SocksProxyError,
    build_socks5_connect_request,
    build_socks5_greeting,
    connect_reply_remaining_length,
    parse_socks5_addr,
    parse_socks5_greeting_reply,
)

pytestmark = pytest.mark.unit


class TestGreeting:
    def test_greeting_bytes(self) -> None:
        assert build_socks5_greeting() == b"\x05\x01\x00"

    def test_accepts_no_auth_reply(self) -> None:
        parse_socks5_greeting_reply(b"\x05\x00")  # no raise

    def test_rejects_wrong_version(self) -> None:
        with pytest.raises(SocksProxyError):
            parse_socks5_greeting_reply(b"\x04\x00")

    def test_rejects_auth_required(self) -> None:
        with pytest.raises(SocksProxyError):
            parse_socks5_greeting_reply(b"\x05\x02")

    def test_rejects_truncated_reply(self) -> None:
        with pytest.raises(SocksProxyError):
            parse_socks5_greeting_reply(b"\x05")


class TestConnectRequest:
    def test_domainname_request_layout(self) -> None:
        req = build_socks5_connect_request("db1.tailxxxx.ts.net", 22)
        assert req[0:4] == b"\x05\x01\x00\x03"
        host_len = req[4]
        host_bytes = req[5 : 5 + host_len]
        assert host_bytes == b"db1.tailxxxx.ts.net"
        port_bytes = req[5 + host_len :]
        assert int.from_bytes(port_bytes, "big") == 22

    def test_rejects_invalid_port(self) -> None:
        with pytest.raises(SocksProxyError):
            build_socks5_connect_request("db1.tailxxxx.ts.net", 0)
        with pytest.raises(SocksProxyError):
            build_socks5_connect_request("db1.tailxxxx.ts.net", 70000)

    def test_rejects_oversized_host(self) -> None:
        with pytest.raises(SocksProxyError):
            build_socks5_connect_request("a" * 256, 22)


class TestConnectReplyRemainingLength:
    def test_ipv4_reply(self) -> None:
        header = bytes([0x05, 0x00, 0x00, 0x01, 0x00])
        assert connect_reply_remaining_length(header) == 3 + 2  # 3 more addr bytes + port

    def test_ipv6_reply(self) -> None:
        header = bytes([0x05, 0x00, 0x00, 0x04, 0x00])
        assert connect_reply_remaining_length(header) == 15 + 2

    def test_domainname_reply_uses_length_byte(self) -> None:
        header = bytes([0x05, 0x00, 0x00, 0x03, 10])
        assert connect_reply_remaining_length(header) == 10 + 2

    def test_rejects_non_success_rep(self) -> None:
        header = bytes([0x05, 0x01, 0x00, 0x01, 0x00])  # REP=general failure
        with pytest.raises(SocksProxyError):
            connect_reply_remaining_length(header)

    def test_rejects_wrong_version(self) -> None:
        header = bytes([0x04, 0x00, 0x00, 0x01, 0x00])
        with pytest.raises(SocksProxyError):
            connect_reply_remaining_length(header)

    def test_rejects_unknown_atyp(self) -> None:
        header = bytes([0x05, 0x00, 0x00, 0x99, 0x00])
        with pytest.raises(SocksProxyError):
            connect_reply_remaining_length(header)

    def test_rejects_truncated_header(self) -> None:
        with pytest.raises(SocksProxyError):
            connect_reply_remaining_length(b"\x05\x00")


class TestParseSocksAddr:
    def test_parses_host_and_port(self) -> None:
        assert parse_socks5_addr("127.0.0.1:1056") == ("127.0.0.1", 1056)

    def test_rejects_missing_port(self) -> None:
        with pytest.raises(SocksProxyError):
            parse_socks5_addr("127.0.0.1")

    def test_rejects_non_numeric_port(self) -> None:
        with pytest.raises(SocksProxyError):
            parse_socks5_addr("127.0.0.1:abc")
