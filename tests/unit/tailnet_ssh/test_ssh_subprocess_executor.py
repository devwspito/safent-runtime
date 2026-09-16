"""SubprocessSshExecutor — argv construction, non-interactive enforcement,
output capping, and timeout handling — against a FAKE `ssh` binary on PATH
(no real ssh, no real network; per session policy, `ssh` is never invoked
against a real host here).
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from hermes.tailnet_ssh.domain.errors import RemoteCommandTimeoutError, SshExecutionError
from hermes.tailnet_ssh.infrastructure.ssh_subprocess_executor import SubprocessSshExecutor

pytestmark = pytest.mark.unit

_FAKE_SSH_SOURCE = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    stdin_data = b""
    if not sys.stdin.isatty():
        stdin_data = sys.stdin.buffer.read()
    record = {{"argv": argv, "stdin": stdin_data.decode("utf-8", "replace")}}
    os.environ.get("FAKE_SSH_RECORD_PATH") and open(
        os.environ["FAKE_SSH_RECORD_PATH"], "w", encoding="utf-8"
    ).write(json.dumps(record))
    sys.stdout.write(os.environ.get("FAKE_SSH_STDOUT", ""))
    sys.stderr.write(os.environ.get("FAKE_SSH_STDERR", ""))
    sys.exit(int(os.environ.get("FAKE_SSH_EXIT_CODE", "0")))
""")


def _write_fake_ssh(tmp_path: Path) -> Path:
    script = tmp_path / "ssh"
    script.write_text(_FAKE_SSH_SOURCE.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _executor(tmp_path: Path, **overrides: object) -> SubprocessSshExecutor:
    kwargs: dict[str, object] = {
        "ssh_binary": str(_write_fake_ssh(tmp_path)),
        "known_hosts_path": tmp_path / "known_hosts",
    }
    kwargs.update(overrides)
    return SubprocessSshExecutor(**kwargs)  # type: ignore[arg-type]


class TestArgvConstruction:
    def test_command_is_a_single_argv_element_no_shell(self, tmp_path: Path, monkeypatch) -> None:
        record_path = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_SSH_RECORD_PATH", str(record_path))
        executor = _executor(tmp_path)

        executor.run(
            host="db1.tailxxxx.ts.net",
            command="echo hi && rm -rf /tmp/x; $(whoami)",
            timeout_s=5,
        )

        record = json.loads(record_path.read_text())
        argv = record["argv"]
        assert argv[-2] == "db1.tailxxxx.ts.net"
        assert argv[-1] == "echo hi && rm -rf /tmp/x; $(whoami)"  # ONE element, untouched

    def test_sets_batch_mode_and_accept_new_and_known_hosts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        record_path = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_SSH_RECORD_PATH", str(record_path))
        known_hosts = tmp_path / "known_hosts"
        executor = _executor(tmp_path, known_hosts_path=known_hosts)

        executor.run(host="db1.tailxxxx.ts.net", command="uptime", timeout_s=5)

        argv = json.loads(record_path.read_text())["argv"]
        opts = " ".join(argv)
        assert "BatchMode=yes" in opts
        assert "StrictHostKeyChecking=accept-new" in opts
        assert f"UserKnownHostsFile={known_hosts}" in opts
        assert "ProxyCommand=" in opts
        assert "socks_proxy_command" in opts

    def test_creates_known_hosts_file_if_missing(self, tmp_path: Path) -> None:
        known_hosts = tmp_path / "nested" / "known_hosts"
        executor = _executor(tmp_path, known_hosts_path=known_hosts)

        executor.run(host="db1.tailxxxx.ts.net", command="uptime", timeout_s=5)

        assert known_hosts.exists()


class TestNonInteractive:
    def test_no_stdin_means_devnull_never_hangs(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        result = executor.run(host="db1.tailxxxx.ts.net", command="uptime", timeout_s=5)
        assert result.exit_code == 0

    def test_stdin_is_forwarded_verbatim(self, tmp_path: Path, monkeypatch) -> None:
        record_path = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_SSH_RECORD_PATH", str(record_path))
        executor = _executor(tmp_path)

        executor.run(host="db1.tailxxxx.ts.net", command="cat", timeout_s=5, stdin="payload")

        assert json.loads(record_path.read_text())["stdin"] == "payload"


class TestOutputCapping:
    def test_output_within_cap_not_truncated(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SSH_STDOUT", "hello")
        executor = _executor(tmp_path)
        result = executor.run(host="db1.tailxxxx.ts.net", command="echo hello", timeout_s=5)
        assert result.stdout == "hello"
        assert result.stdout_truncated is False

    def test_output_over_cap_is_truncated_and_flagged(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SSH_STDOUT", "x" * 1000)
        executor = _executor(tmp_path)
        result = executor.run(
            host="db1.tailxxxx.ts.net", command="yes x", timeout_s=5, max_output_bytes=100
        )
        assert len(result.stdout.encode("utf-8")) <= 100
        assert result.stdout_truncated is True

    def test_stderr_capped_independently_of_stdout(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SSH_STDERR", "e" * 1000)
        executor = _executor(tmp_path)
        result = executor.run(
            host="db1.tailxxxx.ts.net", command="x", timeout_s=5, max_output_bytes=50
        )
        assert result.stderr_truncated is True
        assert result.stdout_truncated is False


class TestExitCodeAndErrors:
    def test_nonzero_exit_code_is_not_an_exception(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SSH_EXIT_CODE", "17")
        executor = _executor(tmp_path)
        result = executor.run(host="db1.tailxxxx.ts.net", command="false", timeout_s=5)
        assert result.exit_code == 17

    def test_missing_ssh_binary_raises(self, tmp_path: Path) -> None:
        executor = SubprocessSshExecutor(
            ssh_binary=str(tmp_path / "does-not-exist"),
            known_hosts_path=tmp_path / "known_hosts",
        )
        with pytest.raises(SshExecutionError):
            executor.run(host="db1.tailxxxx.ts.net", command="uptime", timeout_s=5)

    def test_timeout_raises_remote_command_timeout(self, tmp_path: Path) -> None:
        sleeper = tmp_path / "ssh"
        sleeper.write_text(
            textwrap.dedent(f"""\
                #!{sys.executable}
                import time
                time.sleep(5)
            """),
            encoding="utf-8",
        )
        sleeper.chmod(sleeper.stat().st_mode | stat.S_IEXEC)
        executor = SubprocessSshExecutor(
            ssh_binary=str(sleeper), known_hosts_path=tmp_path / "known_hosts"
        )
        with pytest.raises(RemoteCommandTimeoutError):
            executor.run(host="db1.tailxxxx.ts.net", command="sleep 999", timeout_s=1)
