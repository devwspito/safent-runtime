"""resolve_host — the ONE place that decides whether a raw `host` string is a
real member of the owner's tailnet.

Two, and only two, accepted shapes (spec 022 v2 owner instruction — "never an
IP, never a non-tailnet host"):
  1. A MagicDNS FQDN under the tailnet's own suffix (`<name>.<suffix>`).
  2. A bare peer `name` exactly as listed in `status.json`'s `peers[]`.

Either shape resolves to the SAME canonical value (the full MagicDNS FQDN),
so the per-host allow-list and the audit trail never fork on spelling.
"""

from __future__ import annotations

from hermes.tailnet_ssh.application.ports import TailnetStatus
from hermes.tailnet_ssh.domain.errors import UnknownTailnetHostError
from hermes.tailnet_ssh.domain.host import TailnetHost


def resolve_host(raw_host: object, status: TailnetStatus) -> TailnetHost:
    """Parse + resolve `raw_host` against `status`. Raises
    `InvalidTailnetHostError` (bad format/IP) or `UnknownTailnetHostError`
    (well-formed but not a member of this tailnet)."""
    candidate = TailnetHost.parse(raw_host)
    suffix = status.magicdns_suffix.strip(".").lower()

    if suffix and candidate.value == suffix:
        raise UnknownTailnetHostError(
            f"«{candidate.value}» es el sufijo del tailnet, no un host"
        )
    if suffix and candidate.value.endswith(f".{suffix}"):
        return candidate

    peer_names = {p.name.strip().lower() for p in status.peers if p.name.strip()}
    if candidate.value in peer_names:
        canonical = f"{candidate.value}.{suffix}" if suffix else candidate.value
        return TailnetHost(value=canonical)

    raise UnknownTailnetHostError(
        f"«{candidate.value}» no pertenece al tailnet "
        f"(sufijo «{status.magicdns_suffix}», {len(status.peers)} peers listados)"
    )
