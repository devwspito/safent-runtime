"""RemoteCommand — the command text handed to `ssh` as a SINGLE argv element.

Safent never builds a shell string locally (no `shell=True`, no f-string
concatenation into a command line): the executor passes this value as one
element of an argv list to `ssh`, which itself forwards it to the remote
shell — that is normal, expected ssh behaviour, not local shell expansion.
This VO only rejects the cases that are never legitimate: empty, oversized,
or containing a NUL byte (which no real shell command can contain and which
some subprocess layers mishandle).
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.tailnet_ssh.domain.errors import RemoteCommandRejectedError
from hermes.tailnet_ssh.domain.limits import MAX_COMMAND_CHARS


@dataclass(frozen=True, slots=True)
class RemoteCommand:
    value: str

    @classmethod
    def parse(cls, raw: object) -> RemoteCommand:
        if not isinstance(raw, str):
            raise RemoteCommandRejectedError(
                f"command debe ser texto, recibido {type(raw).__name__}"
            )
        if not raw.strip():
            raise RemoteCommandRejectedError("command vacío")
        if "\x00" in raw:
            raise RemoteCommandRejectedError("command contiene un byte NUL")
        if len(raw) > MAX_COMMAND_CHARS:
            raise RemoteCommandRejectedError(
                f"command excede {MAX_COMMAND_CHARS} caracteres"
            )
        return cls(value=raw)
