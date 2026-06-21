"""Parser del header ``Host`` para HTTP plano y la línea CONNECT.

Extrae el hostname de:
  - HTTP CONNECT:  ``CONNECT example.com:443 HTTP/1.1``
  - HTTP plano:    ``Host: example.com`` (primer header)

No hace red ni I/O.
"""

from __future__ import annotations

from dataclasses import dataclass


class HostParseError(ValueError):
    """No se pudo extraer el hostname de la petición HTTP."""


@dataclass(frozen=True, slots=True)
class ConnectTarget:
    """Destino extraído de una línea CONNECT."""

    host: str
    port: int


@dataclass(frozen=True, slots=True)
class PlainHttpHost:
    """Host extraído de un header ``Host``."""

    host: str
    port: int  # 80 si no se especifica


def parse_connect_line(line: str) -> ConnectTarget:
    """Extrae host:port de una línea ``CONNECT host:port HTTP/1.x``.

    Args:
        line: primera línea de la petición CONNECT (sin \\r\\n).

    Returns:
        ConnectTarget(host, port).

    Raises:
        HostParseError: si el formato no es válido.
    """
    parts = line.strip().split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        raise HostParseError(f"No es una línea CONNECT válida: {line!r}")
    host_port = parts[1]
    return _split_host_port(host_port, default_port=443)


def parse_host_header(header_value: str) -> PlainHttpHost:
    """Extrae host (y puerto opcional) del valor del header ``Host``.

    Args:
        header_value: valor bruto del header ``Host`` (sin el nombre del
                      header ni los dos puntos).  Ejemplos:
                      ``"example.com"``, ``"example.com:8080"``.

    Returns:
        PlainHttpHost(host, port).

    Raises:
        HostParseError: si el valor está vacío o malformado.
    """
    value = header_value.strip()
    if not value:
        raise HostParseError("Header Host vacío")
    target = _split_host_port(value, default_port=80)
    return PlainHttpHost(host=target.host, port=target.port)


def _split_host_port(host_port: str, *, default_port: int) -> ConnectTarget:
    """Separa ``host:port``; soporta IPv6 entre corchetes ``[::1]:443``."""
    if host_port.startswith("["):
        # IPv6 literal: [::1]:port
        bracket_end = host_port.find("]")
        if bracket_end == -1:
            raise HostParseError(f"IPv6 sin corchete de cierre: {host_port!r}")
        host = host_port[1:bracket_end]
        rest = host_port[bracket_end + 1 :]
        if rest.startswith(":"):
            port = _parse_port(rest[1:])
        else:
            port = default_port
    elif ":" in host_port:
        host, _, port_str = host_port.rpartition(":")
        port = _parse_port(port_str)
    else:
        host = host_port
        port = default_port

    host = host.lower().rstrip(".")
    if not host:
        raise HostParseError(f"Hostname vacío en {host_port!r}")
    return ConnectTarget(host=host, port=port)


def _parse_port(port_str: str) -> int:
    try:
        port = int(port_str)
    except ValueError as exc:
        raise HostParseError(f"Puerto no numérico: {port_str!r}") from exc
    if not (1 <= port <= 65535):
        raise HostParseError(f"Puerto fuera de rango: {port}")
    return port
