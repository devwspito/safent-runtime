"""resolve_host — the ONE place that decides whether a raw `host` string is a
real member of the owner's tailnet.

Two, and only two, accepted shapes (spec 022 v2 owner instruction — "never an
IP, never a non-tailnet host"):
  1. A MagicDNS FQDN under the tailnet's own suffix (`<name>.<suffix>`).
  2. A bare peer `name` exactly as listed in `status.json`'s `peers[]`.

Either shape resolves to the SAME canonical value (the full MagicDNS FQDN),
so the per-host allow-list and the audit trail never fork on spelling.

`resolve_host` (below) trusts ANY FQDN under the tailnet's own suffix even
if it is not (yet) in the `peers` snapshot — deliberate, for the
INTERACTIVE/local paths (the owner's own HITL approval, the actual `ssh`
call at execution time): a freshly-joined node can lag the peers snapshot,
and rejecting it would just be an availability papercut for a host the
owner is looking at right now.

`resolve_governed_host` (security review 2026-09, I1/REQ-20) is STRICTER,
for the GOVERNED grant path ONLY (Enterprise's `allow_ssh_host`, applied
without a human looking at a live status page): it requires EXACTLY one
label before the suffix (rejects `a.b.<suffix>`) AND that label to be a
CURRENTLY LISTED peer (rejects `ghost.<suffix>` — any invented subdomain
under the tailnet's own suffix used to resolve without checking
membership at all). A governed grant is the organization declaring ONE
specific machine sight-unseen; the peers-snapshot staleness argument above
does not apply the same way there — REQ-20's "never a non-tailnet host"
is enforced FOR REAL only when the target is checked against `peers`, not
merely against the suffix string.
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


def resolve_governed_host(raw_host: object, status: TailnetStatus) -> TailnetHost:
    """Strict resolution for a CLOUD-governed grant (security review
    2026-09, I1/REQ-20). Unlike `resolve_host`, NEVER trusts a name merely
    for being under the tailnet's own suffix — requires BOTH:
      1. Exactly one label before the suffix (a bare peer name, or
         `<one-label>.<suffix>` — `a.b.<suffix>` is rejected).
      2. That label to be a peer CURRENTLY LISTED in `status.peers`.

    Raises `InvalidTailnetHostError` (bad format/IP) or
    `UnknownTailnetHostError` (well-formed but not a currently-listed peer,
    or more than one label before the suffix) — both PERMANENT rejections
    at the call site (never retried as if the shape might resolve later).
    """
    candidate = TailnetHost.parse(raw_host)
    suffix = status.magicdns_suffix.strip(".").lower()

    bare = candidate.value
    if suffix:
        if bare == suffix:
            raise UnknownTailnetHostError(
                f"«{candidate.value}» es el sufijo del tailnet, no un host"
            )
        if bare.endswith(f".{suffix}"):
            bare = bare[: -(len(suffix) + 1)]

    if not bare or "." in bare:
        raise UnknownTailnetHostError(
            f"«{candidate.value}» no es exactamente una etiqueta antes del "
            f"sufijo del tailnet «{status.magicdns_suffix}»"
        )

    peer_names = {p.name.strip().lower() for p in status.peers if p.name.strip()}
    if bare not in peer_names:
        raise UnknownTailnetHostError(
            f"«{candidate.value}» no es un peer listado en la tailnet "
            f"(sufijo «{status.magicdns_suffix}», {len(status.peers)} peers listados)"
        )

    canonical = f"{bare}.{suffix}" if suffix else bare
    return TailnetHost(value=canonical)
