"""SkillMdDocument — canonical SKILL.md value object (agentskills.io format).

The unified SKILL.md format is the single source of truth for skill content
regardless of origin (autonomous creation via Nous skill_manage, hub install,
or any other governed writer).

Format specification (agentskills.io):
  ---
  name: <skill-name>           # required, matches filesystem dir name
  description: <one-liner>     # required, ≤1024 chars
  version: <semver or int>     # required
  metadata:                    # optional block
    author: <str>
    created_at: <ISO 8601>
    tags: [<str>, ...]
  ---

  ## When
  <trigger conditions — when should the agent use this skill>

  ## Procedure
  <numbered steps>

  ## Pitfalls
  <known failure modes>

  ## Verification
  <how to confirm the skill ran correctly>

Invariants:
  - frontmatter must contain `name` and `description` (both non-empty strings)
  - frontmatter must contain `version` (coerced to str)
  - Body (after closing ---) must be non-empty
  - name is validated against VALID_NAME_RE (filesystem-safe, URL-friendly)

Parsing and YAML (de)serialization live in
``hermes.capabilities.infrastructure.skill_md_codec`` — domain layer stays
pure Python, no I/O, no framework (no ``yaml`` import here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Characters allowed in skill names — mirrors Nous skill_manager_tool.py
VALID_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_NAME_LENGTH: int = 64
MAX_DESCRIPTION_LENGTH: int = 1024


class SkillMdParseError(ValueError):
    """SKILL.md content does not conform to the canonical format."""


@dataclass(frozen=True, slots=True)
class SkillMdDocument:
    """Canonical SKILL.md value object — immutable once constructed."""

    name: str
    description: str
    version: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _assert_valid_name(self.name)
        if not self.description:
            raise SkillMdParseError("description must be non-empty")
        if len(self.description) > MAX_DESCRIPTION_LENGTH:
            raise SkillMdParseError(
                f"description exceeds {MAX_DESCRIPTION_LENGTH} chars"
            )
        if not self.version:
            raise SkillMdParseError("version must be non-empty")
        if not self.body.strip():
            raise SkillMdParseError("body (after frontmatter) must be non-empty")


def _assert_valid_name(name: str) -> None:
    if not name:
        raise SkillMdParseError("name must be non-empty")
    if len(name) > MAX_NAME_LENGTH:
        raise SkillMdParseError(f"name exceeds {MAX_NAME_LENGTH} chars")
    if not VALID_NAME_RE.match(name):
        raise SkillMdParseError(
            f"Invalid skill name {name!r}. "
            "Use lowercase letters, numbers, hyphens, dots, underscores. "
            "Must start with a letter or digit."
        )


def require_str(parsed: dict[str, Any], key: str) -> str:
    """Extract a required non-empty string field — used by the infra parser."""
    value = parsed.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillMdParseError(
            f"frontmatter must include non-empty string field '{key}'"
        )
    return value.strip()
