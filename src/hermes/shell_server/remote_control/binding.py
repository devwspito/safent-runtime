"""FR-055 binding: IP + UA + tenant + operator hash.

Pinned at first use; second use from different IP/UA → reuse_attempt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ClientBinding:
    ip: str
    user_agent: str
    tenant_id: UUID
    operator_id: UUID

    def to_hex(self) -> str:
        h = hashlib.sha256()
        h.update(self.ip.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.user_agent.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.tenant_id.bytes)
        h.update(self.operator_id.bytes)
        return h.hexdigest()


def compute_binding(
    *,
    ip: str,
    user_agent: str,
    tenant_id: UUID,
    operator_id: UUID,
) -> str:
    return ClientBinding(
        ip=ip,
        user_agent=user_agent,
        tenant_id=tenant_id,
        operator_id=operator_id,
    ).to_hex()
