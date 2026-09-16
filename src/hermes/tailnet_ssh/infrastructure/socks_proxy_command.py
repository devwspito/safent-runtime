"""socks_proxy_command — ssh(1) `ProxyCommand` that tunnels through the ops
lane's `tailscaled --socks5-server=127.0.0.1:1056` (host netns, loopback
only). Invoked as `python3 -m hermes.tailnet_ssh.infrastructure.socks_proxy_command
%h %p` — a pure-Python SOCKS5 CONNECT client was chosen over `nc -X 5`
because plain `netcat-openbsd` (what the base image bakes) does NOT support
`-X`/SOCKS proxying; this script only needs the Python interpreter the
daemon already ships, so it is guaranteed present with no new package (see
`specs/022-tailnet-connectivity/ssh-v2.md`).

The SOCKS5 proxy's OWN address (not the ssh target) comes from the
`HERMES_TAILSCALE_SOCKS5_ADDR` env var (set by `SubprocessSshExecutor` on the
ssh child's environment), defaulting to the ops lane's documented
`127.0.0.1:1056`.

Contract: once the SOCKS5 handshake succeeds, this process's stdin/stdout
becomes the raw tunneled byte stream — NOTHING else may ever be written to
stdout (that would corrupt the ssh transport); errors go to stderr only.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading

DEFAULT_SOCKS5_ADDR = "127.0.0.1:1056"
_CONNECT_TIMEOUT_S = 10.0
_SHUTTLE_CHUNK_BYTES = 65536

# SOCKS5 (RFC 1928) wire constants — named so a code reviewer never has to
# guess what a bare byte means.
_SOCKS_VERSION = 0x05
_AUTH_METHOD_NO_AUTH = 0x00
_CMD_CONNECT = 0x01
_ATYP_IPV4 = 0x01
_ATYP_DOMAINNAME = 0x03
_ATYP_IPV6 = 0x04
_REP_SUCCEEDED = 0x00
_MAX_DOMAIN_NAME_BYTES = 255
_MAX_PORT = 65535
_GREETING_REPLY_BYTES = 2
_CONNECT_REPLY_HEADER_BYTES = 5  # VER, REP, RSV, ATYP, + first addr/length byte
_ARGV_COUNT = 3  # program name, target host, target port


class SocksProxyError(Exception):
    """The SOCKS5 handshake with the local tailscaled proxy failed."""


# ---------------------------------------------------------------------------
# Pure framing helpers (independently unit-testable, no socket I/O).
# ---------------------------------------------------------------------------


def build_socks5_greeting() -> bytes:
    """VER=5, NMETHODS=1, METHODS=[NO_AUTH]."""
    return bytes([_SOCKS_VERSION, 0x01, _AUTH_METHOD_NO_AUTH])


def parse_socks5_greeting_reply(data: bytes) -> None:
    if len(data) != _GREETING_REPLY_BYTES:
        raise SocksProxyError(f"greeting reply truncada: {data!r}")
    version, method = data[0], data[1]
    if version != _SOCKS_VERSION:
        raise SocksProxyError(f"versión SOCKS inesperada: {version}")
    if method != _AUTH_METHOD_NO_AUTH:
        raise SocksProxyError(f"el proxy rechazó NO_AUTH (method={method})")


def build_socks5_connect_request(host: str, port: int) -> bytes:
    """VER=5, CMD=CONNECT, RSV=0, ATYP=DOMAINNAME (0x03) — the target is
    always a MagicDNS name, never an IP (tailnet_ssh's own invariant)."""
    host_bytes = host.encode("idna") if _needs_idna(host) else host.encode("ascii")
    if not (1 <= len(host_bytes) <= _MAX_DOMAIN_NAME_BYTES):
        raise SocksProxyError(f"host demasiado largo para SOCKS5: {host!r}")
    if not (0 < port <= _MAX_PORT):
        raise SocksProxyError(f"puerto SOCKS5 inválido: {port}")
    return (
        bytes([_SOCKS_VERSION, _CMD_CONNECT, 0x00, _ATYP_DOMAINNAME])
        + bytes([len(host_bytes)])
        + host_bytes
        + port.to_bytes(2, "big")
    )


def _needs_idna(host: str) -> bool:
    try:
        host.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


# IPv4/IPv6 BND.ADDR length in a CONNECT reply — DOMAINNAME (0x03) is
# variable-length (its own length byte), so it is not in this table.
_ATYP_ADDR_LEN = {_ATYP_IPV4: 4, _ATYP_IPV6: 16}
_BND_PORT_BYTES = 2


def connect_reply_remaining_length(header: bytes) -> int:
    """Given the first 5 bytes of a CONNECT reply (VER, REP, RSV, ATYP,
    first length/addr byte), return how many MORE bytes complete the reply
    (BND.ADDR tail + 2-byte BND.PORT). Raises on a REP != succeeded (0x00)."""
    if len(header) < _CONNECT_REPLY_HEADER_BYTES:
        raise SocksProxyError("connect reply truncada")
    version, rep, _rsv, atyp = header[0], header[1], header[2], header[3]
    if version != _SOCKS_VERSION:
        raise SocksProxyError(f"versión SOCKS inesperada en connect reply: {version}")
    if rep != _REP_SUCCEEDED:
        raise SocksProxyError(f"SOCKS5 CONNECT rechazado (REP={rep})")
    if atyp in _ATYP_ADDR_LEN:
        return _ATYP_ADDR_LEN[atyp] - 1 + _BND_PORT_BYTES  # -1: byte[4] already consumed
    if atyp == _ATYP_DOMAINNAME:  # byte[4] IS the length prefix
        return header[4] + _BND_PORT_BYTES
    raise SocksProxyError(f"ATYP desconocido en connect reply: {atyp}")


def parse_socks5_addr(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    if not host or not port.isdigit():
        raise SocksProxyError(f"HERMES_TAILSCALE_SOCKS5_ADDR inválida: {addr!r}")
    return host, int(port)


# ---------------------------------------------------------------------------
# I/O — socket handshake + stdio shuttle.
# ---------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksProxyError("el proxy SOCKS5 cerró la conexión antes de tiempo")
        buf.extend(chunk)
    return bytes(buf)


def do_socks5_handshake(sock: socket.socket, target_host: str, target_port: int) -> None:
    sock.sendall(build_socks5_greeting())
    parse_socks5_greeting_reply(_recv_exact(sock, _GREETING_REPLY_BYTES))

    sock.sendall(build_socks5_connect_request(target_host, target_port))
    header = _recv_exact(sock, _CONNECT_REPLY_HEADER_BYTES)
    remaining = connect_reply_remaining_length(header)
    if remaining:
        _recv_exact(sock, remaining)


def _shuttle(sock: socket.socket) -> None:
    """Bidirectional copy: sock<->stdio, until either side closes."""

    def _sock_to_stdout() -> None:
        with contextlib.suppress(OSError):
            while True:
                chunk = sock.recv(_SHUTTLE_CHUNK_BYTES)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()

    def _stdin_to_sock() -> None:
        try:
            while True:
                chunk = sys.stdin.buffer.read(_SHUTTLE_CHUNK_BYTES)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)

    reader = threading.Thread(target=_sock_to_stdout, daemon=True)
    reader.start()
    _stdin_to_sock()
    reader.join()


def main(argv: list[str]) -> int:
    if len(argv) != _ARGV_COUNT:
        print("usage: socks_proxy_command <target-host> <target-port>", file=sys.stderr)
        return 2
    target_host, target_port = argv[1], int(argv[2])
    proxy_host, proxy_port = parse_socks5_addr(
        os.environ.get("HERMES_TAILSCALE_SOCKS5_ADDR", DEFAULT_SOCKS5_ADDR)
    )
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=_CONNECT_TIMEOUT_S)
    except OSError as exc:
        print(
            f"no se pudo conectar al proxy SOCKS5 {proxy_host}:{proxy_port}: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        do_socks5_handshake(sock, target_host, target_port)
    except SocksProxyError as exc:
        print(f"handshake SOCKS5 fallido: {exc}", file=sys.stderr)
        sock.close()
        return 1
    sock.settimeout(None)
    _shuttle(sock)
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
