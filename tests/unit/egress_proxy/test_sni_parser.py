"""Tests del parser SNI — bytes fijos, sin red.

Cubre:
  - parse_sni con un ClientHello real de ejemplo (bytes fijos).
  - SNIParseError cuando el fragmento es demasiado corto.
  - SNIParseError cuando el content_type no es 0x16.
  - SNIParseError cuando no hay extensión SNI.
"""

from __future__ import annotations

import pytest

from hermes.egress_proxy.domain.sni_parser import SNIParseError, TLSClientHello, parse_sni

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# ClientHello real capturado con wireshark + python -c
# openssl s_client -connect example.com:443 -msg 2>/dev/null | head -50
# Construido a mano para que los tests sean autocontenidos (sin red).
# ---------------------------------------------------------------------------


def _build_client_hello(sni: str) -> bytes:
    """Construye un ClientHello TLS 1.2 mínimo válido con el SNI dado.

    Este es el formato canónico que parse_sni() debe consumir.
    Es idéntico al que emite cualquier cliente TLS moderno.
    """
    # SNI extension body
    sni_bytes = sni.encode("ascii")
    sni_name_entry = (
        b"\x00"  # name_type: host_name
        + len(sni_bytes).to_bytes(2, "big")
        + sni_bytes
    )
    sni_list = len(sni_name_entry).to_bytes(2, "big") + sni_name_entry
    sni_ext = (
        b"\x00\x00"  # extension_type: server_name (0x0000)
        + len(sni_list).to_bytes(2, "big")
        + sni_list
    )

    # Extensions block
    extensions = sni_ext
    ext_block = len(extensions).to_bytes(2, "big") + extensions

    # ClientHello body:
    # client_version (TLS 1.2 = 0x0303)
    # random (32 bytes de zeros)
    # session_id_length = 0
    # cipher_suites: [TLS_RSA_WITH_AES_128_CBC_SHA = 0x002F]
    # compression_methods: [null = 0x00]
    # extensions
    client_hello_body = (
        b"\x03\x03"          # client_version TLS 1.2
        + b"\x00" * 32       # random
        + b"\x00"            # session_id_length = 0
        + b"\x00\x02"        # cipher_suites_length = 2
        + b"\x00\x2f"        # TLS_RSA_WITH_AES_128_CBC_SHA
        + b"\x01"            # compression_methods_length = 1
        + b"\x00"            # compression null
        + ext_block
    )

    # Handshake header:
    # handshake_type = 0x01 (ClientHello)
    # length (3 bytes)
    hs_body = (
        b"\x01"
        + len(client_hello_body).to_bytes(3, "big")
        + client_hello_body
    )

    # TLS Record:
    # content_type = 0x16 (Handshake)
    # version = TLS 1.0 (0x0301) for compat
    # length (2 bytes)
    record = (
        b"\x16"
        + b"\x03\x01"
        + len(hs_body).to_bytes(2, "big")
        + hs_body
    )
    return record


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestParseSniHappyPath:
    def test_example_com(self) -> None:
        data = _build_client_hello("example.com")
        result = parse_sni(data)
        assert isinstance(result, TLSClientHello)
        assert result.sni == "example.com"

    def test_uppercase_normalized_to_lowercase(self) -> None:
        data = _build_client_hello("Example.COM")
        result = parse_sni(data)
        assert result.sni == "example.com"

    def test_subdomain(self) -> None:
        data = _build_client_hello("api.service.internal.example.com")
        result = parse_sni(data)
        assert result.sni == "api.service.internal.example.com"

    def test_trailing_dot_stripped(self) -> None:
        # Algunos clientes incluyen punto final en el SNI
        data = _build_client_hello("example.com.")
        result = parse_sni(data)
        assert result.sni == "example.com"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestParseSniErrors:
    def test_too_short_raises(self) -> None:
        with pytest.raises(SNIParseError):
            parse_sni(b"\x16\x03\x01")  # solo 3 bytes

    def test_wrong_content_type_raises(self) -> None:
        data = _build_client_hello("example.com")
        # Cambia content_type de 0x16 a 0x17 (Application Data)
        bad = b"\x17" + data[1:]
        with pytest.raises(SNIParseError, match="content_type"):
            parse_sni(bad)

    def test_wrong_handshake_type_raises(self) -> None:
        data = _build_client_hello("example.com")
        # El handshake_type está en el byte 5 (offset tras record header)
        bad = data[:5] + b"\x02" + data[6:]  # 0x02 = ServerHello
        with pytest.raises(SNIParseError, match="Handshake type"):
            parse_sni(bad)

    def test_empty_bytes_raises(self) -> None:
        with pytest.raises(SNIParseError):
            parse_sni(b"")

    def test_no_sni_extension(self) -> None:
        """ClientHello con extensión desconocida (type 0xFFFF) — sin SNI."""
        # Construimos un ClientHello con una extensión que NO es SNI
        ext_body = b"\xff\xff\x00\x00"  # type=0xFFFF, length=0
        ext_block = len(ext_body).to_bytes(2, "big") + ext_body
        client_hello_body = (
            b"\x03\x03"
            + b"\x00" * 32
            + b"\x00"
            + b"\x00\x02"
            + b"\x00\x2f"
            + b"\x01"
            + b"\x00"
            + ext_block
        )
        hs_body = b"\x01" + len(client_hello_body).to_bytes(3, "big") + client_hello_body
        record = b"\x16\x03\x01" + len(hs_body).to_bytes(2, "big") + hs_body
        with pytest.raises(SNIParseError, match="SNI"):
            parse_sni(record)
