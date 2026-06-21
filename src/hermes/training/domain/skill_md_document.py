"""SkillMdDocument — canonical SKILL.md value object (agentskills.io format).

The unified SKILL.md format is the single source of truth for skill content
regardless of origin (autonomous creation via Nous skill_manage, or human
teaching via SkillCompiler).

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
  - Must start with --- (YAML frontmatter)
  - frontmatter must contain `name` and `description` (both non-empty strings)
  - frontmatter must contain `version` (coerced to str)
  - Body (after closing ---) must be non-empty
  - name is validated against VALID_NAME_RE (filesystem-safe, URL-friendly)

Parse/serialize are inverses: parse(serialize(doc)) == doc (up to whitespace).

Domain layer: pure Python, no I/O, no framework.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

# Characters allowed in skill names — mirrors Nous skill_manager_tool.py
VALID_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_NAME_LENGTH: int = 64
MAX_DESCRIPTION_LENGTH: int = 1024


class SkillMdParseError(ValueError):
    """SKILL.md content does not conform to the canonical format."""


@dataclass(frozen=True, slots=True)
class SkillMdDocument:
    """Canonical SKILL.md value object — immutable once constructed.

    Both skill origins produce this artifact:
      - Nous autonomous path: parsed from skill_manage parameters["content"]
      - Human teaching path: serialized from SkillCompiler output
    """

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

    def serialize(self) -> str:
        """Render canonical SKILL.md text."""
        fm: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }
        if self.metadata:
            fm["metadata"] = self.metadata

        frontmatter = yaml.dump(fm, default_flow_style=False, allow_unicode=True).rstrip()
        return f"---\n{frontmatter}\n---\n\n{self.body.strip()}\n"

    def content_bytes(self) -> bytes:
        """UTF-8 encoded canonical representation — used for content_hash."""
        return self.serialize().encode("utf-8")


def parse_skill_md(content: str) -> SkillMdDocument:
    """Parse SKILL.md text into a SkillMdDocument.

    Raises:
        SkillMdParseError: if the content does not conform to the format.
    """
    if not content.strip():
        raise SkillMdParseError("SKILL.md content is empty")
    if not content.startswith("---"):
        raise SkillMdParseError(
            "SKILL.md must start with YAML frontmatter (---)"
        )

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        raise SkillMdParseError(
            "SKILL.md frontmatter not closed — missing closing '---' line"
        )

    yaml_src = content[3 : end_match.start() + 3]
    try:
        parsed = yaml.safe_load(yaml_src)
    except yaml.YAMLError as exc:
        raise SkillMdParseError(f"YAML frontmatter parse error: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SkillMdParseError("frontmatter must be a YAML mapping")

    name = _require_str(parsed, "name")
    description = _require_str(parsed, "description")
    version = str(parsed.get("version", "")).strip()
    if not version:
        raise SkillMdParseError("frontmatter must include 'version' field")

    metadata = parsed.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    body = content[end_match.end() + 3 :].strip()
    if not body:
        raise SkillMdParseError(
            "SKILL.md must have content after the frontmatter"
        )

    return SkillMdDocument(
        name=name,
        description=description,
        version=version,
        body=body,
        metadata=dict(metadata),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


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


def _require_str(parsed: dict[str, Any], key: str) -> str:
    value = parsed.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillMdParseError(
            f"frontmatter must include non-empty string field '{key}'"
        )
    return value.strip()
