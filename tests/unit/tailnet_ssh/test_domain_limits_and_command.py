"""RemoteCommand / RemotePath / validate_timeout — the hard caps."""

from __future__ import annotations

import pytest

from hermes.tailnet_ssh.domain.command import RemoteCommand
from hermes.tailnet_ssh.domain.errors import RemoteCommandRejectedError, RemotePathRejectedError
from hermes.tailnet_ssh.domain.limits import MAX_COMMAND_CHARS, validate_timeout
from hermes.tailnet_ssh.domain.remote_path import RemotePath

pytestmark = pytest.mark.unit


class TestValidateTimeout:
    @pytest.mark.parametrize("value", [1, 30, 300])
    def test_accepts_in_range(self, value: int) -> None:
        assert validate_timeout(value) == value

    @pytest.mark.parametrize("value", [0, -1, 301, 100_000])
    def test_rejects_out_of_range(self, value: int) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            validate_timeout(value)

    @pytest.mark.parametrize("value", ["30", 30.0, None, True, False])
    def test_rejects_non_int(self, value: object) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            validate_timeout(value)


class TestRemoteCommand:
    def test_accepts_arbitrary_text(self) -> None:
        assert RemoteCommand.parse("systemctl status foo").value == "systemctl status foo"

    def test_rejects_empty(self) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            RemoteCommand.parse("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            RemoteCommand.parse("   ")

    def test_rejects_nul_byte(self) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            RemoteCommand.parse("echo hi\x00; rm -rf /")

    def test_rejects_oversized(self) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            RemoteCommand.parse("x" * (MAX_COMMAND_CHARS + 1))

    def test_rejects_non_string(self) -> None:
        with pytest.raises(RemoteCommandRejectedError):
            RemoteCommand.parse(["ls", "-la"])  # type: ignore[arg-type]

    def test_preserves_shell_metacharacters_verbatim(self) -> None:
        """The VO does NOT sanitize shell syntax — that is normal ssh remote-
        command behaviour, not local shell expansion (see module docstring).
        Injection safety comes from argv-list dispatch, not text-stripping."""
        raw = "echo hi && rm -rf /tmp/x; echo $(whoami)"
        assert RemoteCommand.parse(raw).value == raw


class TestRemotePath:
    def test_accepts_normal_path(self) -> None:
        assert RemotePath.parse("/etc/hostname").value == "/etc/hostname"

    def test_rejects_empty(self) -> None:
        with pytest.raises(RemotePathRejectedError):
            RemotePath.parse("")

    def test_rejects_newline(self) -> None:
        with pytest.raises(RemotePathRejectedError):
            RemotePath.parse("/etc/hostname\nrm -rf /")

    def test_rejects_nul(self) -> None:
        with pytest.raises(RemotePathRejectedError):
            RemotePath.parse("/etc/host\x00name")
