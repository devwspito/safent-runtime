"""SKILL.md YAML (de)serialization — infrastructure adapter for SkillMdDocument.

Split out of the domain value object (``capabilities.domain.skill_md_document``)
so the domain layer stays framework-free: YAML parsing/dumping is a framework
concern and lives here, per DDD layering.
"""

from __future__ import annotations

import re

import yaml

from hermes.capabilities.domain.skill_md_document import (
    SkillMdDocument,
    SkillMdParseError,
    require_str,
)


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

    end_match = _find_frontmatter_end(content)
    if end_match is None:
        raise SkillMdParseError(
            "SKILL.md frontmatter not closed — missing closing '---' line"
        )

    yaml_src = content[3 : end_match.start() + 3]
    parsed = _load_frontmatter(yaml_src)

    name = require_str(parsed, "name")
    description = require_str(parsed, "description")
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


def serialize_skill_md(doc: SkillMdDocument) -> str:
    """Render the canonical SKILL.md text for *doc*."""
    fm: dict[str, object] = {
        "name": doc.name,
        "description": doc.description,
        "version": doc.version,
    }
    if doc.metadata:
        fm["metadata"] = doc.metadata

    frontmatter = yaml.dump(fm, default_flow_style=False, allow_unicode=True).rstrip()
    return f"---\n{frontmatter}\n---\n\n{doc.body.strip()}\n"


def skill_md_content_bytes(doc: SkillMdDocument) -> bytes:
    """UTF-8 encoded canonical representation — used for content_hash."""
    return serialize_skill_md(doc).encode("utf-8")


def _find_frontmatter_end(content: str) -> "re.Match[str] | None":
    return re.search(r"\n---\s*\n", content[3:])


def _load_frontmatter(yaml_src: str) -> dict:
    try:
        parsed = yaml.safe_load(yaml_src)
    except yaml.YAMLError as exc:
        raise SkillMdParseError(f"YAML frontmatter parse error: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillMdParseError("frontmatter must be a YAML mapping")
    return parsed
