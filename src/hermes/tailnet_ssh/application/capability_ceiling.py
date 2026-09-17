"""resolve_execution_identity — the ONE place that turns a resolved host's
`HostGrant` into an execution identity + a pass/fail decision for one
capability (spec 002 US3, D-4 — governed tailnet SSH capability ceiling).

Shared by TailnetSshUseCase (capability="exec"), TailnetFileGetUseCase
(capability="file_read"), and TailnetFilePutUseCase (capability="file_write")
so the three never independently reimplement (and risk drifting on) the same
ceiling check.

Local/unmanaged hosts (`grant` is None or `grant.managed_by` is not
"cloud") have NO ceiling — today's unrestricted behaviour, unchanged: the
returned identity is None (the executor's own ambient default).
"""

from __future__ import annotations

from hermes.tailnet_ssh.application.ports import HostGrant
from hermes.tailnet_ssh.domain.errors import SshCapabilityDeniedError
from hermes.tailnet_ssh.domain.remote_identity import RemoteIdentity

_CLOUD_MANAGED = "cloud"


def resolve_execution_identity(
    grant: HostGrant | None, *, host: str, capability: str
) -> str | None:
    """Return the ssh login identity to use for `host`, having verified that
    `capability` is within the host's ceiling.

    Raises `SshCapabilityDeniedError` when `grant` is cloud-managed and does
    NOT include `capability` — the caller must audit the denial and never
    reach the executor. Raises `InvalidRemoteIdentityError` (also a
    `TailnetSshError`) if the stored identity itself is malformed — fail-
    closed rather than pass it to `ssh` verbatim (defense-in-depth: the
    allow-list file is its own trust boundary, see RemoteIdentity's
    docstring). Returns None (no forced identity) for any non-cloud-managed
    host — today's behaviour, unchanged.
    """
    if grant is None or grant.managed_by != _CLOUD_MANAGED:
        return None

    granted = grant.capabilities or frozenset()
    if capability not in granted:
        raise SshCapabilityDeniedError(
            f"«{capability}» no está autorizado en «{host}» "
            f"(capacidades concedidas: {sorted(granted)})"
        )
    return RemoteIdentity.parse(grant.identity).value
