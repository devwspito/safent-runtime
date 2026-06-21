"""Application-layer ports (interfaces) for the Security Center.

Concrete adapters live in infrastructure/. The application layer only imports these.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_record import ScanRecord
from hermes.security_center.domain.scan_score import Risk


@runtime_checkable
class IScanner(Protocol):
    """A single-concern scanner that returns a list of risks for a target.

    name:    Unique scanner identifier matching a key in policy.scanner_weights.
    enabled: When False, the scanner is skipped and contributes a full weight score.
    """

    name: str

    async def scan(self, target: InstallTarget) -> list[Risk]:
        """Return zero or more Risk findings. Must not raise — return [] on error."""
        ...


@runtime_checkable
class IScanHistoryRepo(Protocol):
    """Persistence port for ScanRecord objects."""

    def save(self, record: ScanRecord) -> None: ...

    def get(self, scan_id: UUID) -> ScanRecord | None: ...

    def get_by_cache_key(self, cache_key: str) -> ScanRecord | None:
        """Return the most recent cached record for this key, or None."""
        ...

    def list_recent(self, *, limit: int) -> list[ScanRecord]: ...

    def update_decision(self, scan_id: UUID, decision: str) -> None: ...


@runtime_checkable
class IPolicyRepo(Protocol):
    """Persistence port for the single SecurityPolicy record."""

    def load(self) -> SecurityPolicy: ...

    def save(self, policy: SecurityPolicy) -> None: ...
