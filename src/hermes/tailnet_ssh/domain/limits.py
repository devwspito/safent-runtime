"""Hard caps for tailnet_ssh — the numbers a code reviewer needs to see in
one place. No magic numbers scattered across the module."""

from __future__ import annotations

from hermes.tailnet_ssh.domain.errors import RemoteCommandRejectedError

MIN_TIMEOUT_S: int = 1
MAX_TIMEOUT_S: int = 300

# Captured stdout/stderr per stream, for tailnet_ssh.
MAX_OUTPUT_BYTES: int = 256 * 1024

# stdin fed to the remote command (tailnet_ssh's optional `stdin` arg).
MAX_STDIN_BYTES: int = 1024 * 1024

# tailnet_file_get / tailnet_file_put — deliberately small (this is a governed
# remote-exec side channel, not a file-transfer product).
MAX_FILE_BYTES: int = 5 * 1024 * 1024

MAX_COMMAND_CHARS: int = 8192
MAX_REMOTE_PATH_CHARS: int = 4096


def validate_timeout(timeout_s: object) -> int:
    """Fail-closed: anything that is not a plain int in [1, 300] is rejected."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
        raise RemoteCommandRejectedError(
            f"timeout_s debe ser un entero, recibido {type(timeout_s).__name__}"
        )
    if not (MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S):
        raise RemoteCommandRejectedError(
            f"timeout_s={timeout_s} fuera de rango [{MIN_TIMEOUT_S}, {MAX_TIMEOUT_S}]"
        )
    return timeout_s
