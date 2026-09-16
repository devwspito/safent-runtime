"""Integration: the FULL chain a real ssh(1) would drive — SubprocessSshExecutor
builds argv/env -> a FAKE `ssh` binary on PATH parses `-o ProxyCommand=...`
EXACTLY like OpenSSH does, substitutes %h/%p, and execs it -> the REAL
`socks_proxy_command` module -> a fake SOCKS5 listener (loopback only, no
real tailscaled, no real sshd, no real remote host — per session policy).

This is what proves the ProxyCommand string SubprocessSshExecutor constructs
is actually consumable by ssh's own `-o` option grammar, not just internally
self-consistent.
"""

from __future__ import annotations

import json
import socket
import stat
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from hermes.tailnet_ssh.infrastructure.ssh_subprocess_executor import SubprocessSshExecutor

pytestmark = pytest.mark.integration

# A fake `ssh` that understands JUST ENOUGH of the real grammar to prove the
# executor's argv is consumable: parses `-o key=value` pairs (including
# ProxyCommand, whose value may itself contain spaces/%h/%p), finds the `--`
# sentinel, then treats the remaining two args as (host, command). It runs
# the substituted ProxyCommand as ssh itself would, feeding it one line and
# reading the echo back — proving the REAL socks_proxy_command module can be
# launched from an argv shaped exactly like SubprocessSshExecutor's.
_FAKE_SSH_WITH_PROXY_SOURCE = textwrap.dedent("""\
    #!{python}
    import json, os, subprocess, sys

    argv = sys.argv[1:]
    opts = {{}}
    i = 0
    positional = []
    while i < len(argv):
        arg = argv[i]
        if arg == "-o":
            key, _, val = argv[i + 1].partition("=")
            opts[key] = val
            i += 2
        elif arg == "--":
            positional = argv[i + 1:]
            break
        else:
            i += 1

    host, command = positional[0], positional[1]
    proxy_template = opts.get("ProxyCommand", "")
    proxy_argv = proxy_template.replace("%h", host).replace("%p", "22").split(" ")

    proxy = subprocess.run(
        proxy_argv, input=b"ping-through-real-proxy-command",
        capture_output=True, timeout=10, env=os.environ,
    )
    sys.stdout.write(json.dumps({{
        "remote_command": command,
        "proxy_exit_code": proxy.returncode,
        "proxy_stdout": proxy.stdout.decode("utf-8", "replace"),
    }}))
    sys.exit(0)
""")


def _write_fake_ssh(tmp_path: Path) -> Path:
    script = tmp_path / "ssh"
    script.write_text(_FAKE_SSH_WITH_PROXY_SOURCE.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


class _FakeSocks5EchoServer:
    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.accepted_target: tuple[str, int] | None = None
        threading.Thread(target=self._serve_one, daemon=True).start()

    def _serve_one(self) -> None:
        conn, _ = self._listener.accept()
        try:
            conn.recv(3)
            conn.sendall(b"\x05\x00")
            header = conn.recv(4)
            length = conn.recv(1)[0] if header[3] == 0x03 else 0
            host = conn.recv(length).decode("ascii") if length else ""
            port = int.from_bytes(conn.recv(2), "big")
            self.accepted_target = (host, port)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(data)
        finally:
            conn.close()
            self._listener.close()


class TestSubprocessSshExecutorFullChainThroughRealProxyCommand:
    def test_ssh_argv_drives_a_real_proxy_command_to_a_fake_socks5_listener(
        self, tmp_path: Path
    ) -> None:
        socks_server = _FakeSocks5EchoServer()
        executor = SubprocessSshExecutor(
            ssh_binary=str(_write_fake_ssh(tmp_path)),
            known_hosts_path=tmp_path / "known_hosts",
            socks5_addr=f"127.0.0.1:{socks_server.port}",
        )

        result = executor.run(
            host="db1.tailxxxx.ts.net", command="uptime", timeout_s=10
        )

        payload = json.loads(result.stdout)
        assert payload["remote_command"] == "uptime"
        assert payload["proxy_exit_code"] == 0
        assert payload["proxy_stdout"] == "ping-through-real-proxy-command"
        assert socks_server.accepted_target == ("db1.tailxxxx.ts.net", 22)
