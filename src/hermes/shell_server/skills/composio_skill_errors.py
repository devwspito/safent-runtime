"""Typed errors for the Composio skill creation path."""

from __future__ import annotations


class ComposioCredentialMissing(Exception):
    """No Composio API key is configured in the DB."""


class ComposioToolkitNotConnected(Exception):
    """The requested toolkit is not in the user's ACTIVE connected accounts."""

    def __init__(self, toolkit_slug: str) -> None:
        super().__init__(
            f"Toolkit {toolkit_slug!r} is not connected. "
            "Connect it first via POST /api/v1/integrations/composio/connect."
        )
        self.toolkit_slug = toolkit_slug


class ComposioSkillNameConflict(Exception):
    """A skill with the same skill_id + version already exists."""

    def __init__(self, skill_name: str, version: int) -> None:
        super().__init__(
            f"Composio skill {skill_name!r} version {version} already exists."
        )
        self.skill_name = skill_name
        self.version = version


class ComposioSkillValidationError(ValueError):
    """Input validation failed for a Composio skill field."""
