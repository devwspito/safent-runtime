"""Tests for native hermes-agent skill discovery (TAREA 3).

Covers the _list_native_hermes_agent_skills function that surfaces
SKILL.md files from $HERMES_HOME/skills/ in the ListSkills D-Bus response.

The disconnect being fixed:
  - hermes-agent's skill_manage writes SKILL.md to $HERMES_HOME/skills/<name>/
    without touching the DB (skill_packages_view).
  - ListSkills only queried the DB → showed "No hay skills" despite files on disk.
  - Fix: _list_native_hermes_agent_skills() scans the directory and merges results.

Tests use the skills_root= parameter to isolate from the real filesystem.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    _list_native_hermes_agent_skills,
    _extract_skill_description,
    _iso_mtime,
)

pytestmark = pytest.mark.unit


def _write_skill(skills_root: Path, name: str, description: str = "") -> Path:
    """Write a minimal SKILL.md at skills_root/<name>/SKILL.md."""
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: {name}\n"
    if description:
        frontmatter += f"description: {description}\n"
    frontmatter += "---\n\nDoes something.\n"
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(frontmatter, encoding="utf-8")
    return skill_file


class TestListNativeHermesAgentSkills:
    def test_returns_empty_when_skills_root_missing(self, tmp_path: Path) -> None:
        result = _list_native_hermes_agent_skills([], skills_root=tmp_path / "nonexistent")
        assert result == []

    def test_returns_empty_when_no_hermes_home_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
        # No skills_root override → falls back to env which is not set → []
        result = _list_native_hermes_agent_skills([])
        assert result == []

    def test_returns_skill_from_disk(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "my-skill")

        result = _list_native_hermes_agent_skills([], skills_root=tmp_path)

        assert len(result) == 1
        assert result[0]["skill_name"] == "my-skill"
        assert result[0]["state"] == "native"
        assert result[0]["source"] == "hermes_agent"
        assert result[0]["signing_method"] == "none"

    def test_returns_multiple_skills(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "skill-a")
        _write_skill(tmp_path, "skill-b")

        result = _list_native_hermes_agent_skills([], skills_root=tmp_path)

        names = {r["skill_name"] for r in result}
        assert names == {"skill-a", "skill-b"}

    def test_db_skill_excludes_disk_skill_with_same_name(
        self, tmp_path: Path
    ) -> None:
        _write_skill(tmp_path, "existing-skill")
        db_skills = [{"skill_name": "existing-skill", "state": "autonomous"}]

        result = _list_native_hermes_agent_skills(db_skills, skills_root=tmp_path)

        assert result == []

    def test_only_unregistered_disk_skills_returned(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "in-db-skill")
        _write_skill(tmp_path, "native-only-skill")
        db_skills = [{"skill_name": "in-db-skill", "state": "validated"}]

        result = _list_native_hermes_agent_skills(db_skills, skills_root=tmp_path)

        assert len(result) == 1
        assert result[0]["skill_name"] == "native-only-skill"

    def test_extracts_description_from_frontmatter(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "described-skill", description="Handles emails")

        result = _list_native_hermes_agent_skills([], skills_root=tmp_path)

        assert result[0]["description"] == "Handles emails"

    def test_skill_without_description_has_empty_string(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "no-desc")

        result = _list_native_hermes_agent_skills([], skills_root=tmp_path)

        assert result[0]["description"] == ""

    def test_signed_at_is_iso_string(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "ts-skill")

        result = _list_native_hermes_agent_skills([], skills_root=tmp_path)

        signed_at = result[0]["signed_at"]
        assert signed_at  # non-empty
        assert "T" in signed_at  # ISO-8601 basic format

    def test_package_id_prefixed_with_native(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "pfx-skill")

        result = _list_native_hermes_agent_skills([], skills_root=tmp_path)

        assert result[0]["package_id"].startswith("native:")

    def test_hermes_home_env_used_when_no_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _write_skill(skills_root, "env-skill")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = _list_native_hermes_agent_skills([])

        assert len(result) == 1
        assert result[0]["skill_name"] == "env-skill"


class TestExtractSkillDescription:
    def test_extracts_description_field(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: foo\ndescription: Does foo things\n---\nContent.\n")
        assert _extract_skill_description(f) == "Does foo things"

    def test_returns_empty_when_no_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("Just plain text, no frontmatter.\n")
        assert _extract_skill_description(f) == ""

    def test_returns_empty_when_no_description_key(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: bar\n---\nContent.\n")
        assert _extract_skill_description(f) == ""

    def test_returns_empty_on_missing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.md"
        assert _extract_skill_description(f) == ""

    def test_strips_double_quotes_from_description(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text('---\nname: baz\ndescription: "Quoted desc"\n---\n\n')
        assert _extract_skill_description(f) == "Quoted desc"

    def test_truncates_long_description(self, tmp_path: Path) -> None:
        long_desc = "x" * 300
        f = tmp_path / "SKILL.md"
        f.write_text(f"---\nname: long\ndescription: {long_desc}\n---\n\n")
        result = _extract_skill_description(f)
        assert len(result) <= 200


class TestIsoMtime:
    def test_returns_iso_string_for_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("x")
        result = _iso_mtime(f)
        assert "T" in result

    def test_returns_empty_string_for_missing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.txt"
        assert _iso_mtime(f) == ""
