"""Regression tests for the 3 security findings from the coordinator's review.

Finding #1 [MEDIUM, CWE-345]: _skill_md_to_dto must re-verify HMAC, not trust
    self-asserted state/signing_method/signature_hex from frontmatter verbatim.

Finding #2 [LOW, functional regression]: promote_skill must work for cage-signed
    skills that exist only on disk (SkillStoreAdapter no longer writes to
    skill_packages_view, so the old SELECT returns None).

Finding #3 [LOW, path traversal] was in _persist_as_skill_md (training/persist.py),
    retired with the teach-by-browser feature on 10-sep-2026 — see
    specs/025-safent-repaso/retirada-ensenar.md. SkillStoreAdapter (the surviving
    write path) structurally cannot regress it: SkillMdDocument.__post_init__
    enforces VALID_NAME_RE on every parse_skill_md() call, before any filesystem
    write (capabilities/domain/skill_md_document.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared fake key / vault
# ---------------------------------------------------------------------------

_FAKE_KEY = b"\xCC" * 32


class _FakeVault:
    def derive_subkey(self, *, label: str) -> bytes:  # noqa: ARG002
        return _FAKE_KEY


def _fake_vault_patch():
    import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415
    return patch.object(_mod, "SecretsVault", return_value=_FakeVault())


def _compute_valid_hmac(*, payload_dict: dict) -> str:
    canonical = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_FAKE_KEY, canonical, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Finding #1 — HMAC re-verification in _skill_md_to_dto
# ---------------------------------------------------------------------------


class TestFinding1HmacReverification:
    """_skill_md_to_dto must not trust self-asserted state/signing_method/signature_hex."""

    def _write_cage_skill_md(
        self,
        skills_root: Path,
        name: str,
        *,
        signature_hex: str,
        extra_meta: dict | None = None,
    ) -> Path:
        """Write a SKILL.md with v2 governance and the given signature_hex."""
        import yaml as _yaml  # noqa: PLC0415

        skill_dir = skills_root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        package_id = str(uuid4())
        skill_id = str(uuid4())
        signed_at = "2026-06-25T00:00:00+00:00"
        replay_id = str(uuid4())
        voice_id = str(uuid4())
        meta: dict = {
            "package_id": package_id,
            "skill_id": skill_id,
            "state": "validated",
            "signing_method": "v2",
            "signature_hex": signature_hex,
            "signed_at": signed_at,
            "validated_at": signed_at,
            "surface_kinds": ["skill_store"],
            "version": 1,
            # HMAC payload fields (Finding #1)
            "content_hash": "c" * 64,
            "tenant_id": str(uuid4()),
            "compiled_by_operator_id": str(uuid4()),
            "created_at": signed_at,
            "runtime_version": "test",
            "replay_script_id": replay_id,
            "voice_narrative_id": voice_id,
            "decision_rule_ids": [],
        }
        if extra_meta:
            meta.update(extra_meta)
        fm = {
            "name": name,
            "description": "Security finding test skill",
            "version": "1",
            "metadata": meta,
        }
        content = f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- always\n\n## Procedure\n1. run\n"
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(content)
        return skill_file

    def test_forged_signature_downgrades_to_unverified(self, tmp_path: Path) -> None:
        """A SKILL.md with state='validated' but a random 64-char signature must be
        listed as state='unverified' when the native key is available (CWE-345)."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _list_native_skills_primary,
        )
        skills_root = tmp_path / "skills"
        # Use an incorrect signature (random hex, not computed with _FAKE_KEY)
        fake_sig = "deadbeef" * 8  # 64 chars but wrong HMAC

        self._write_cage_skill_md(skills_root, "forged-skill", signature_hex=fake_sig)

        with _fake_vault_patch():
            skills = _list_native_skills_primary(skills_root=skills_root)

        skill = next((s for s in skills if s["skill_name"] == "forged-skill"), None)
        assert skill is not None, "forged skill must still be listed (BUG 3 fix preserved)"
        assert skill["state"] == "unverified", (
            "forged signature must downgrade state to 'unverified', got: " + skill["state"]
        )
        assert skill["source"] == "disk", (
            "forged signature must downgrade source to 'disk', got: " + skill["source"]
        )

    def test_correct_signature_stays_validated(self, tmp_path: Path) -> None:
        """A SKILL.md with a correctly computed v2 HMAC must stay as state='validated'."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _list_native_skills_primary,
        )
        skills_root = tmp_path / "skills"
        package_id = str(uuid4())
        skill_id = str(uuid4())
        signed_at = "2026-06-25T00:00:00+00:00"
        replay_id = str(uuid4())
        voice_id = str(uuid4())
        tenant_id = str(uuid4())
        compiled_by = str(uuid4())
        content_hash = "c" * 64
        runtime_version = "test"

        # Compute the correct HMAC using _FAKE_KEY
        payload_dict = {
            "replay_script_id": replay_id,
            "decision_rule_ids": [],
            "voice_narrative_id": voice_id,
            "content_hash": content_hash,
            "tenant_id": tenant_id,
            "compiled_by_operator_id": compiled_by,
            "created_at": signed_at,
            "runtime_version": runtime_version,
        }
        correct_sig = _compute_valid_hmac(payload_dict=payload_dict)

        import yaml as _yaml  # noqa: PLC0415
        skill_dir = skills_root / "valid-sig-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "package_id": package_id,
            "skill_id": skill_id,
            "state": "validated",
            "signing_method": "v2",
            "signature_hex": correct_sig,
            "signed_at": signed_at,
            "validated_at": signed_at,
            "surface_kinds": ["skill_store"],
            "version": 1,
            "content_hash": content_hash,
            "tenant_id": tenant_id,
            "compiled_by_operator_id": compiled_by,
            "created_at": signed_at,
            "runtime_version": runtime_version,
            "replay_script_id": replay_id,
            "voice_narrative_id": voice_id,
            "decision_rule_ids": [],
        }
        fm = {
            "name": "valid-sig-skill",
            "description": "Valid HMAC skill",
            "version": "1",
            "metadata": meta,
        }
        (skill_dir / "SKILL.md").write_text(
            f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- always\n\n## Procedure\n1. run\n"
        )

        with _fake_vault_patch():
            skills = _list_native_skills_primary(skills_root=skills_root)

        skill = next((s for s in skills if s["skill_name"] == "valid-sig-skill"), None)
        assert skill is not None
        assert skill["state"] == "validated", (
            "correct signature must keep state='validated', got: " + skill["state"]
        )
        assert skill["source"] == "cage"

    def test_missing_payload_fields_downgrades(self, tmp_path: Path) -> None:
        """A SKILL.md with signing_method=v2 but missing HMAC payload fields (old format)
        must be downgraded to 'unverified' when the key is available."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _list_native_skills_primary,
        )
        import yaml as _yaml  # noqa: PLC0415

        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "missing-fields-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        # No HMAC payload fields (as would be written by the pre-fix code)
        meta = {
            "state": "validated",
            "signing_method": "v2",
            "signature_hex": "e" * 64,
            "signed_at": "2026-06-25T00:00:00+00:00",
        }
        fm = {
            "name": "missing-fields-skill",
            "description": "Skill without HMAC payload",
            "version": "1",
            "metadata": meta,
        }
        (skill_dir / "SKILL.md").write_text(
            f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- always\n\n## Procedure\n1. run\n"
        )

        with _fake_vault_patch():
            skills = _list_native_skills_primary(skills_root=skills_root)

        skill = next((s for s in skills if s["skill_name"] == "missing-fields-skill"), None)
        assert skill is not None, "skill must still be listed even with missing payload fields"
        assert skill["state"] == "unverified", (
            "missing HMAC payload fields with key present → 'unverified', got: " + skill["state"]
        )

    def test_key_unavailable_keeps_original_state(self, tmp_path: Path) -> None:
        """When the native keystore is unavailable (no master.key in CI), the skill
        must be listed with its on-disk state (cannot forge without the key)."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _list_native_skills_primary,
        )
        skills_root = tmp_path / "skills"
        self._write_cage_skill_md(skills_root, "no-key-skill", signature_hex="f" * 64)

        # No vault patch — NativeKeyStoreAdapter raises SigningKeyError (no master.key)
        skills = _list_native_skills_primary(skills_root=skills_root)

        skill = next((s for s in skills if s["skill_name"] == "no-key-skill"), None)
        assert skill is not None
        # Cannot verify, cannot forge — keep as written (not downgraded)
        assert skill["state"] == "validated"


# ---------------------------------------------------------------------------
# Finding #2 — promote_skill for disk-based cage-signed skills
# ---------------------------------------------------------------------------


class TestFinding2PromoteDiskSkill:
    """promote_skill must work for cage-signed skills that exist only on disk."""

    def _write_disk_skill_with_valid_hmac(
        self,
        tmp_path: Path,
        package_id: str,
    ) -> dict:
        """Write a SKILL.md with a valid HMAC under tmp_path/skills/. Returns the meta dict."""
        import yaml as _yaml  # noqa: PLC0415

        skill_id = str(uuid4())
        signed_at = "2026-06-25T00:00:00+00:00"
        replay_id = str(uuid4())
        voice_id = str(uuid4())
        tenant_id = str(uuid4())
        compiled_by = str(uuid4())
        content_hash = "d" * 64
        runtime_version = "test"

        payload_dict = {
            "replay_script_id": replay_id,
            "decision_rule_ids": [],
            "voice_narrative_id": voice_id,
            "content_hash": content_hash,
            "tenant_id": tenant_id,
            "compiled_by_operator_id": compiled_by,
            "created_at": signed_at,
            "runtime_version": runtime_version,
        }
        signature_hex = _compute_valid_hmac(payload_dict=payload_dict)

        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "cage-only-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "package_id": package_id,
            "skill_id": skill_id,
            "state": "validated",
            "signing_method": "v2",
            "signature_hex": signature_hex,
            "signed_at": signed_at,
            "validated_at": signed_at,
            "surface_kinds": ["skill_store"],
            "version": 1,
            "content_hash": content_hash,
            "tenant_id": tenant_id,
            "compiled_by_operator_id": compiled_by,
            "created_at": signed_at,
            "runtime_version": runtime_version,
            "replay_script_id": replay_id,
            "voice_narrative_id": voice_id,
            "decision_rule_ids": [],
        }
        fm = {
            "name": "cage-only-skill",
            "description": "Cage skill on disk only",
            "version": "1",
            "metadata": meta,
        }
        (skill_dir / "SKILL.md").write_text(
            f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- always\n\n## Procedure\n1. run\n"
        )
        return meta

    async def test_promote_disk_skill_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """promote_skill must succeed for a cage-signed skill on disk (not in DB)."""
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = tmp_path / "test.db"
        package_id = str(uuid4())
        self._write_disk_skill_with_valid_hmac(tmp_path, package_id)

        svc = SkillGovernanceService(db_path=db)
        with _fake_vault_patch():
            result = await svc.promote_skill(
                package_id=package_id,
                promoted_by=uuid4(),
            )

        assert result["state"] == "autonomous", (
            f"disk-based cage skill must be promotable, got state: {result['state']}"
        )
        assert result["promoted_at"] is not None

    async def test_promote_disk_skill_with_bad_signature_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """promote_skill must reject a disk skill with an invalid signature (fail-closed)."""
        import yaml as _yaml  # noqa: PLC0415
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = tmp_path / "test.db"
        package_id = str(uuid4())

        # Write SKILL.md with all payload fields but a WRONG signature
        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "bad-sig-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "package_id": package_id,
            "skill_id": str(uuid4()),
            "state": "validated",
            "signing_method": "v2",
            "signature_hex": "b" * 64,  # wrong
            "signed_at": "2026-06-25T00:00:00+00:00",
            "surface_kinds": ["skill_store"],
            "version": 1,
            "content_hash": "c" * 64,
            "tenant_id": str(uuid4()),
            "compiled_by_operator_id": str(uuid4()),
            "created_at": "2026-06-25T00:00:00+00:00",
            "runtime_version": "test",
            "replay_script_id": str(uuid4()),
            "voice_narrative_id": str(uuid4()),
            "decision_rule_ids": [],
        }
        fm = {
            "name": "bad-sig-skill",
            "description": "Bad signature disk skill",
            "version": "1",
            "metadata": meta,
        }
        (skill_dir / "SKILL.md").write_text(
            f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- always\n\n## Procedure\n1. run\n"
        )

        svc = SkillGovernanceService(db_path=db)
        with _fake_vault_patch(), pytest.raises(SkillSignatureVerificationFailed):
            await svc.promote_skill(package_id=package_id, promoted_by=uuid4())

    async def test_promote_disk_skill_missing_payload_fields_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """promote_skill must fail for a disk skill that lacks HMAC payload fields (old format)."""
        import yaml as _yaml  # noqa: PLC0415
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = tmp_path / "test.db"
        package_id = str(uuid4())

        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "old-format-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        # Old format: no HMAC payload fields, just the base governance
        meta = {
            "package_id": package_id,
            "skill_id": str(uuid4()),
            "state": "validated",
            "signing_method": "v2",
            "signature_hex": "a" * 64,
            "signed_at": "2026-06-25T00:00:00+00:00",
        }
        fm = {
            "name": "old-format-skill",
            "description": "Old format skill",
            "version": "1",
            "metadata": meta,
        }
        (skill_dir / "SKILL.md").write_text(
            f"---\n{_yaml.dump(fm).rstrip()}\n---\n\n## When\n- always\n\n## Procedure\n1. run\n"
        )

        svc = SkillGovernanceService(db_path=db)
        with _fake_vault_patch(), pytest.raises(SkillSignatureVerificationFailed):
            await svc.promote_skill(package_id=package_id, promoted_by=uuid4())
