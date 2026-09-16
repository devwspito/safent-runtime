"""SubprocessSshExecutor — the ONLY place that actually spawns `ssh`.

Runs in the daemon (host netns). Identity = Tailscale SSH (the tailnet ACL
authorises the node; no SSH keypair): `BatchMode=yes` (never prompts),
`StrictHostKeyChecking=accept-new` against a Safent-owned known_hosts file
(never the operator's `~/.ssh/known_hosts`). Reaches the target ONLY through
the ops lane's `tailscaled --socks5-server=127.0.0.1:1056` (host-netns
loopback, per spec 022) via `socks_proxy_command` as `ProxyCommand`.

`command` is passed as ONE argv element — never interpolated into a shell
string on the Safent side (`subprocess.run` with a list, no `shell=True`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from hermes.tailnet_ssh.application.ports import SshExecutionResult
from hermes.tailnet_ssh.domain.errors import RemoteCommandTimeoutError, SshExecutionError
from hermes.tailnet_ssh.domain.limits import MAX_OUTPUT_BYTES
from hermes.tailnet_ssh.infrastructure.socks_proxy_command import (
    DEFAULT_SOCKS5_ADDR,
)

DEFAULT_KNOWN_HOSTS_PATH = Path("/var/lib/hermes/tailscale/known_hosts")
_DEFAULT_PROXY_COMMAND_MODULE = "hermes.tailnet_ssh.infrastructure.socks_proxy_command"
_MAX_CONNECT_TIMEOUT_S = 30


class SubprocessSshExecutor:
    def __init__(
        self,
        *,
        ssh_binary: str | None = None,
        known_hosts_path: Path = DEFAULT_KNOWN_HOSTS_PATH,
        socks5_addr: str = DEFAULT_SOCKS5_ADDR,
        proxy_command_module: str = _DEFAULT_PROXY_COMMAND_MODULE,
        python_executable: str | None = None,
    ) -> None:
        self._ssh_binary_override = ssh_binary
        self._known_hosts_path = known_hosts_path
        self._socks5_addr = socks5_addr
        self._proxy_command_module = proxy_command_module
        self._python_executable = python_executable or sys.executable

    def run(
        self,
        *,
        host: str,
        command: str,
        timeout_s: int,
        stdin: str | None = None,
        max_output_bytes: int | None = None,
    ) -> SshExecutionResult:
        ssh_binary = self._ssh_binary_override or shutil.which("ssh")
        if not ssh_binary:
            raise SshExecutionError("binario «ssh» no encontrado en PATH")
        self._ensure_known_hosts()

        cap = max_output_bytes if max_output_bytes is not None else MAX_OUTPUT_BYTES
        argv = self._build_argv(ssh_binary, host, command, timeout_s)
        env = {**os.environ, "HERMES_TAILSCALE_SOCKS5_ADDR": self._socks5_addr}

        started = time.monotonic()
        try:
            completed = self._spawn(argv, stdin=stdin, timeout_s=timeout_s, env=env)
        except subprocess.TimeoutExpired as exc:
            raise RemoteCommandTimeoutError(
                f"ssh a «{host}» excedió {timeout_s}s"
            ) from exc
        except OSError as exc:
            raise SshExecutionError(f"no se pudo lanzar «ssh»: {exc}") from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        stdout, stdout_truncated = _cap(completed.stdout, cap)
        stderr, stderr_truncated = _cap(completed.stderr, cap)
        return SshExecutionResult(
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_ms=duration_ms,
        )

    def _spawn(
        self, argv: list[str], *, stdin: str | None, timeout_s: int, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        """Always non-interactive: DEVNULL when no stdin was requested, never
        the daemon's own inherited stdin (ssh forwards it to the remote
        command unless explicitly redirected — BatchMode only suppresses
        prompts). check=False: a non-zero remote exit code is a normal
        RESULT, never a Safent-side error — the caller reads it off
        `.returncode`."""
        if stdin is not None:
            return subprocess.run(  # noqa: S603 — argv list, no shell=True
                argv, input=stdin.encode("utf-8"), capture_output=True,
                timeout=timeout_s, env=env, check=False,
            )
        return subprocess.run(  # noqa: S603 — argv list, no shell=True
            argv, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout_s, env=env, check=False,
        )

    def _build_argv(
        self, ssh_binary: str, host: str, command: str, timeout_s: int
    ) -> list[str]:
        proxy_command = (
            f"{self._python_executable} -m {self._proxy_command_module} %h %p"
        )
        connect_timeout = max(1, min(timeout_s, _MAX_CONNECT_TIMEOUT_S))
        return [
            ssh_binary,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self._known_hosts_path}",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", f"ProxyCommand={proxy_command}",
            "--",
            host,
            command,
        ]

    def _ensure_known_hosts(self) -> None:
        self._known_hosts_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._known_hosts_path.exists():
            self._known_hosts_path.touch(mode=0o600)


def _cap(data: bytes, cap: int) -> tuple[str, bool]:
    truncated = len(data) > cap
    return data[:cap].decode("utf-8", "replace"), truncated
