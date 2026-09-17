"""RemoteIdentity — a validated remote SSH login name (spec 002 US3, D-4).

Defense-in-depth: `HostGrant.identity` already passed Enterprise's
`SshHostSpec.identity` pattern once, at parse time, before it was ever
persisted to the allow-list JSON file. This VO re-validates it at the OTHER
end — reading persisted state back out is its own trust boundary (a
corrupted/tampered file is not the same guarantee as a freshly-verified
Ed25519-signed bundle) — before it is ever handed to the executor as an
`ssh -l` argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hermes.tailnet_ssh.domain.errors import InvalidRemoteIdentityError

# Mirrors SshHostSpec.identity's pattern in hermes.config_sync.policy_document.
_IDENTITY_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    value: str

    @classmethod
    def parse(cls, raw: object) -> RemoteIdentity:
        if not isinstance(raw, str) or not _IDENTITY_RE.match(raw):
            raise InvalidRemoteIdentityError(
                f"identity «{raw!r}» no es un nombre de usuario remoto válido"
            )
        return cls(value=raw)
