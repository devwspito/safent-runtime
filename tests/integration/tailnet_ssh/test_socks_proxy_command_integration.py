"""Integration: `socks_proxy_command` against a REAL loopback TCP socket
implementing SOCKS5 (no real tailscaled, no real ssh, no real remote host —
a fake SOCKS5 listener only). Marked `integration` (loopback network I/O);
excluded from the default unit gate.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.integration


def _subprocess_env(**overrides: str) -> dict[str, str]:
    """The daemon's real env, MINUS whatever HERMES_TAILSCALE_SOCKS5_ADDR was
    already set to — this test always injects its own fake-listener address."""
    env = {**os.environ}
    env.update(overrides)
    return env


class _FakeSocks5EchoServer:
    """Accepts ONE connection, completes a SOCKS5 CONNECT handshake, records
    the requested target, then echoes whatever bytes it receives — enough to
    prove the client's shuttle is bidirectional and byte-exact."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.accepted_target: tuple[str, int] | None = None
        self._thread = threading.Thread(target=self._serve_one, daemon=True)
        self._thread.start()

    def _serve_one(self) -> None:
        conn, _ = self._listener.accept()
        try:
            conn.recv(3)  # greeting: VER, NMETHODS, METHODS
            conn.sendall(b"\x05\x00")

            header = conn.recv(4)
            atyp = header[3]
            if atyp == 0x03:
                length = conn.recv(1)[0]
                host = conn.recv(length).decode("ascii")
            else:  # pragma: no cover — this test only ever sends DOMAINNAME
                host = ""
            port = int.from_bytes(conn.recv(2), "big")
            self.accepted_target = (host, port)

            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success, 0.0.0.0:0

            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(data)
        finally:
            conn.close()
            self._listener.close()

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)


class TestSocksProxyCommandAgainstFakeListener:
    def test_handshake_and_echo_round_trip(self) -> None:
        server = _FakeSocks5EchoServer()
        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes.tailnet_ssh.infrastructure.socks_proxy_command",
             "db1.tailxxxx.ts.net", "22"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_subprocess_env(HERMES_TAILSCALE_SOCKS5_ADDR=f"127.0.0.1:{server.port}"),
        )

        stdout, stderr = proc.communicate(input=b"hello over the tailnet", timeout=10)
        server.join()

        assert proc.returncode == 0, stderr.decode("utf-8", "replace")
        assert stdout == b"hello over the tailnet"
        assert server.accepted_target == ("db1.tailxxxx.ts.net", 22)

    def test_proxy_refused_reports_nonzero_and_no_stdout_corruption(self) -> None:
        # No listener at all on this port — connection refused.
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()  # freed immediately: nothing listens there

        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes.tailnet_ssh.infrastructure.socks_proxy_command",
             "db1.tailxxxx.ts.net", "22"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_subprocess_env(HERMES_TAILSCALE_SOCKS5_ADDR=f"127.0.0.1:{port}"),
        )
        stdout, stderr = proc.communicate(timeout=10)

        assert proc.returncode != 0
        assert stdout == b""  # never corrupts the tunnel with an error message


class TestSocksProxyCommandRejectsBadProxyReply:
    def test_general_failure_reply_exits_nonzero(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def _serve_rejecting() -> None:
            conn, _ = listener.accept()
            conn.recv(3)
            conn.sendall(b"\x05\x00")
            conn.recv(4096)
            conn.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")  # REP=general failure
            conn.close()
            listener.close()

        threading.Thread(target=_serve_rejecting, daemon=True).start()

        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes.tailnet_ssh.infrastructure.socks_proxy_command",
             "denied-host.tailxxxx.ts.net", "22"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_subprocess_env(HERMES_TAILSCALE_SOCKS5_ADDR=f"127.0.0.1:{port}"),
        )
        stdout, stderr = proc.communicate(timeout=10)

        assert proc.returncode == 1
        assert stdout == b""
        assert b"SOCKS5" in stderr
