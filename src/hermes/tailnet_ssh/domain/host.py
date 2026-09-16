"""TailnetHost — a validated, non-IP hostname candidate.

This value object only performs FORMAT validation (never an IP literal,
never a URL/scheme, valid hostname grammar). Whether the host actually
belongs to the owner's tailnet (MagicDNS suffix match or listed peer name)
requires the live directory (`status.json`) and is resolved one layer up, in
`hermes.tailnet_ssh.application.host_resolution.resolve_host`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from hermes.tailnet_ssh.domain.errors import InvalidTailnetHostError

# RFC 1123 hostname label grammar (each dot-separated label): alnum, hyphens
# in the middle, 1-63 chars. No scheme, no port, no path, no whitespace.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


@dataclass(frozen=True, slots=True)
class TailnetHost:
    """A syntactically valid, lowercase hostname. NEVER an IP literal."""

    value: str

    @classmethod
    def parse(cls, raw: object) -> TailnetHost:
        if not isinstance(raw, str):
            raise InvalidTailnetHostError(
                f"host debe ser texto, recibido {type(raw).__name__}"
            )
        candidate = raw.strip()
        if not candidate:
            raise InvalidTailnetHostError("host vacío")
        candidate = candidate.lower()
        if _looks_like_ip_literal(candidate):
            raise InvalidTailnetHostError(
                f"host «{candidate}» es una IP — solo se aceptan nombres MagicDNS"
            )
        if not _HOSTNAME_RE.match(candidate):
            raise InvalidTailnetHostError(f"host «{candidate}» no es un hostname válido")
        return cls(value=candidate)


def _looks_like_ip_literal(candidate: str) -> bool:
    is_bracketed = candidate.startswith("[") and candidate.endswith("]")
    stripped = candidate[1:-1] if is_bracketed else candidate
    try:
        ipaddress.ip_address(stripped)
        return True
    except ValueError:
        return False
