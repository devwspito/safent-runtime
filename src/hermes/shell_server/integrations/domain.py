"""Domain: integration configuration persisted in shell-state.db.

An Integration represents a third-party service connected to Hermes via a
cloud bridge (currently Composio).  The only secret we store is our own
Composio API key — we NEVER store OAuth tokens for the user's apps; those
live exclusively in Composio cloud.

One row per `kind` (UNIQUE constraint).  Currently only kind='composio'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Integration:
    """Persisted configuration for one integration kind.

    Fields:
        kind:       Integration kind identifier (e.g. 'composio').
        has_api_key: True when an encrypted API key is stored.
        enabled:    Whether the integration is active.
        entity_id:  Default Composio entity_id used for connected accounts.
        created_at: Creation timestamp.
    """

    kind: str
    has_api_key: bool
    enabled: bool
    entity_id: str
    created_at: datetime


def composio_integration(
    *,
    has_api_key: bool = False,
    enabled: bool = True,
    entity_id: str = "default",
) -> Integration:
    """Factory for a new Composio Integration record."""
    return Integration(
        kind="composio",
        has_api_key=has_api_key,
        enabled=enabled,
        entity_id=entity_id,
        created_at=datetime.now(tz=UTC),
    )


class IntegrationNotFound(Exception):
    """Raised when no Integration row matches the requested kind."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"Integration not found: {kind!r}")
        self.kind = kind
