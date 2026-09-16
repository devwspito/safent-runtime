"""RemotePath — a validated remote filesystem path for tailnet_file_get/put.

Unlike RemoteCommand (deliberately arbitrary), Safent BUILDS the remote
command around this value (`cat -- <path>` / `cat > <path>`), so it is
shell-quoted by the executor before being interpolated — this VO rejects the
obviously-wrong shapes up front (empty, NUL, newline, oversized) so a bad
path fails loud at the domain boundary instead of producing a confusing
remote error.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.tailnet_ssh.domain.errors import RemotePathRejectedError
from hermes.tailnet_ssh.domain.limits import MAX_REMOTE_PATH_CHARS


@dataclass(frozen=True, slots=True)
class RemotePath:
    value: str

    @classmethod
    def parse(cls, raw: object) -> RemotePath:
        if not isinstance(raw, str):
            raise RemotePathRejectedError(
                f"path debe ser texto, recibido {type(raw).__name__}"
            )
        candidate = raw.strip()
        if not candidate:
            raise RemotePathRejectedError("path vacío")
        if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
            raise RemotePathRejectedError("path contiene un byte de control")
        if len(candidate) > MAX_REMOTE_PATH_CHARS:
            raise RemotePathRejectedError(
                f"path excede {MAX_REMOTE_PATH_CHARS} caracteres"
            )
        return cls(value=candidate)
