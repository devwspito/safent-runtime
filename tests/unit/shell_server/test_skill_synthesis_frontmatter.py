"""Regresión 2026-06-28 (cazado en verificación LIVE de R4):

La síntesis web ahora delega la persistencia en el SkillStoreAdapter nativo, cuyo
`SkillMdDocument` EXIGE name/description/version en el frontmatter. El LLM suele
emitir solo `description` → el adapter rechazaba ("frontmatter must include ...").
`_ensure_frontmatter_fields` garantiza los tres campos sin fiarnos del modelo.
"""
from __future__ import annotations

import pytest

from hermes.shell_server.skills.skill_synthesis import (
    _ensure_frontmatter_fields,
    slugify,
)

pytestmark = pytest.mark.unit


def test_injects_name_and_version_when_only_description() -> None:
    c = "---\ndescription: saluda formal\n---\n\n# Saluda\ncuerpo"
    out = _ensure_frontmatter_fields(c, "Saluda Nativo", "desc larga")
    assert "name: saluda-nativo" in out
    assert "version: 1" in out
    assert "description: saluda formal" in out  # no pisa la existente
    assert out.lstrip().startswith("---")


def test_does_not_duplicate_existing_fields() -> None:
    c = "---\nname: ya-esta\ndescription: x\nversion: 3\n---\ncuerpo"
    assert _ensure_frontmatter_fields(c, "Otro", "d") == c


def test_no_frontmatter_builds_full_block() -> None:
    out = _ensure_frontmatter_fields("# Solo cuerpo", "Mi Skill", "una desc")
    assert "name: mi-skill" in out
    assert "version: 1" in out
    assert "description: una desc" in out
    assert "# Solo cuerpo" in out


def test_slugify_filesystem_safe() -> None:
    assert slugify("Saluda Formal!! 2") == "saluda-formal-2"
    assert slugify("").startswith("skill-")  # fallback no vacío
