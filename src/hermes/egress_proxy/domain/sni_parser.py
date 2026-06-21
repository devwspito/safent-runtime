"""Parser de SNI (Server Name Indication) de un ClientHello TLS.

Extrae el hostname del registro TLS sin descifrar ni avanzar la
conexión — solo lectura del primer fragmento que el cliente envía.

NO se hace MITM.  El propósito es exclusivamente leer el SNI en claro
para decidir si el CONNECT (ya gateado por dominio del header HTTP) debe
continuar, o bien para registrar el destino real cuando el CONNECT lleva
un host de CDN (IP cruda) y el SNI revela el nombre real.

Referencia: RFC 6066 §3 — TLS Extensions: Server Name.
"""

from __future__ import annotations

from dataclasses import dataclass


class SNIParseError(ValueError):
    """No se pudo extraer el SNI del fragmento recibido."""


@dataclass(frozen=True, slots=True)
class TLSClientHello:
    """Resultado del parseo de un ClientHello TLS 1.x."""

    sni: str  # hostname en minúsculas, sin punto final


def parse_sni(data: bytes) -> TLSClientHello:
    """Extrae el SNI de los primeros bytes de un ClientHello TLS.

    Args:
        data: fragmento de bytes recibido del cliente (>= 5 bytes del
              record header + el payload del ClientHello).

    Returns:
        TLSClientHello con el hostname SNI.

    Raises:
        SNIParseError: si el fragmento no contiene SNI válido o es
                       demasiado corto para parsearse.

    El parseo es defensivo: cualquier truncamiento o campo malformado
    lanza SNIParseError en lugar de IndexError/struct.error silencioso.
    """
    try:
        return _parse(data)
    except (IndexError, struct_unpack_error, UnicodeDecodeError) as exc:
        raise SNIParseError(f"No SNI found in ClientHello fragment: {exc}") from exc


# ---------------------------------------------------------------------------
# Implementación interna
# ---------------------------------------------------------------------------

# Constantes del protocolo TLS
_TLS_CONTENT_TYPE_HANDSHAKE: int = 0x16
_HANDSHAKE_TYPE_CLIENT_HELLO: int = 0x01
_EXTENSION_TYPE_SERVER_NAME: int = 0x00
_NAME_TYPE_HOST_NAME: int = 0x00


class struct_unpack_error(Exception):
    """Sentinel para re-raise limpio."""


def _parse(data: bytes) -> TLSClientHello:
    """Parseo interno — eleva IndexError / UnicodeDecodeError en corrupción."""
    offset = 0

    # --- TLS Record header (5 bytes) ---
    if len(data) < 5:
        raise SNIParseError("Demasiado corto para record header TLS")
    content_type = data[offset]
    if content_type != _TLS_CONTENT_TYPE_HANDSHAKE:
        raise SNIParseError(
            f"Record content_type={content_type:#04x}, esperado 0x16 (Handshake)"
        )
    # version (2 bytes): aceptamos cualquier valor (TLS 1.0–1.3 legítimos)
    offset += 3
    record_length = _u16(data, offset)
    offset += 2

    if len(data) < offset + record_length:
        raise SNIParseError("Fragmento truncado: record_length excede datos disponibles")

    # --- Handshake header (4 bytes) ---
    handshake_type = data[offset]
    if handshake_type != _HANDSHAKE_TYPE_CLIENT_HELLO:
        raise SNIParseError(
            f"Handshake type={handshake_type:#04x}, esperado 0x01 (ClientHello)"
        )
    offset += 1
    # handshake length (3 bytes big-endian)
    _hs_length = (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
    offset += 3

    # --- ClientHello body ---
    # client_version (2) + random (32) + session_id_length (1) + session_id
    offset += 2 + 32
    session_id_len = data[offset]
    offset += 1 + session_id_len

    # cipher_suites
    cipher_suites_len = _u16(data, offset)
    offset += 2 + cipher_suites_len

    # compression_methods
    compression_len = data[offset]
    offset += 1 + compression_len

    # extensions length
    if offset + 2 > len(data):
        raise SNIParseError("Sin campo de longitud de extensiones — SNI ausente")
    extensions_len = _u16(data, offset)
    offset += 2

    ext_end = offset + extensions_len
    if ext_end > len(data):
        raise SNIParseError("Extensiones truncadas")

    # Recorre extensiones hasta encontrar SNI (type 0x0000)
    while offset + 4 <= ext_end:
        ext_type = _u16(data, offset)
        offset += 2
        ext_len = _u16(data, offset)
        offset += 2

        if ext_type == _EXTENSION_TYPE_SERVER_NAME:
            return _parse_sni_extension(data, offset, ext_len)

        offset += ext_len

    raise SNIParseError("Extensión SNI no encontrada en ClientHello")


def _parse_sni_extension(data: bytes, offset: int, ext_len: int) -> TLSClientHello:
    """Parsea el cuerpo de la extensión SNI."""
    # server_name_list_length (2)
    list_len = _u16(data, offset)
    offset += 2

    end = offset + list_len
    while offset + 3 <= end:
        name_type = data[offset]
        offset += 1
        name_len = _u16(data, offset)
        offset += 2

        if name_type == _NAME_TYPE_HOST_NAME:
            hostname_bytes = data[offset : offset + name_len]
            hostname = hostname_bytes.decode("ascii").lower().rstrip(".")
            if not hostname:
                raise SNIParseError("SNI hostname vacío")
            return TLSClientHello(sni=hostname)

        offset += name_len

    raise SNIParseError("No se encontró entrada hostname_name en SNI list")


def _u16(data: bytes, offset: int) -> int:
    """Lee un entero big-endian de 2 bytes en ``offset``."""
    return (data[offset] << 8) | data[offset + 1]
