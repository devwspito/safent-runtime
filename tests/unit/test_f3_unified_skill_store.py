"""F3 — Unified skill store tests.

Covers the mandatory test cases:
  (a) skill_manage create via broker → SKILL.md written + signed v2 (not unsigned).
  (b) Unsigned/v1/manipulated skill → not loaded/executed (fail-closed).
  (d) Both paths write to the same store + same governance gate.
  (e) Progressive loading works for signed skills.

(c) — skill_compiler (teaching path) parity — removed with the dead
``hermes.training`` GEPA subtree (unreachable from every real entrypoint;
oleada 1 lane L1c). ``hermes.agents_os.application.skill_compiler``, the
former teaching-path compiler, lost its ``.compile()`` when teach-by-browser
was retired 10-sep-2026 (specs/025-safent-repaso/retirada-ensenar.md); its
``SkillPackage``/``.verify()`` survive for skill_replay, unrelated to this
module's own ``SkillPackage`` (capabilities.domain.skill_package).
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.infrastructure.skill_store_adapter import SkillStoreAdapter
from hermes.shell_server.skills.skill_governance_service import (
    SkillGovernanceService,
    SkillSignatureVerificationFailed,
)
from hermes.capabilities.application.skill_signer import (
    KmsSigningKeyPort,
    SignatureVerificationError,
    SkillSigner,
    verify_skill_signature,
)
from hermes.capabilities.domain.skill_md_document import (
    SkillMdDocument,
    SkillMdParseError,
)
from hermes.capabilities.domain.skill_package import SkillPackage
from hermes.capabilities.domain.skill_state import SkillState
from hermes.capabilities.infrastructure.skill_md_codec import (
    parse_skill_md,
    serialize_skill_md,
    skill_md_content_bytes,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

_FAKE_KEY = b"hermes-test-signing-key-32bytes!"
_KEY_ID = "skill-signing-v2"


class _InMemoryKms:
    """Fake KMS — returns a stable key without master.key."""

    async def get_signing_key(self, *, tenant_id: object, key_id: str) -> bytes:  # noqa: ARG002
        return _FAKE_KEY


def _audit_db_schema() -> str:
    return """
    CREATE TABLE IF NOT EXISTS skill_packages_view (
      package_id         TEXT PRIMARY KEY,
      skill_id           TEXT NOT NULL,
      skill_name         TEXT NOT NULL,
      version            INTEGER NOT NULL,
      state              TEXT NOT NULL,
      surface_kinds      TEXT NOT NULL,
      signed_at          TEXT NOT NULL,
      signature_short    TEXT,
      validated_at       TEXT,
      validated_by       TEXT,
      promoted_at        TEXT,
      promoted_by        TEXT,
      signing_method     TEXT NOT NULL DEFAULT 'v1',
      signature_hex      TEXT
    );
    CREATE INDEX IF NOT EXISTS skill_state_idx
      ON skill_packages_view (state, signed_at DESC);

    CREATE TABLE IF NOT EXISTS composio_skills (
      package_id   TEXT PRIMARY KEY,
      toolkit_slug TEXT NOT NULL,
      intent_text  TEXT NOT NULL,
      created_at   TEXT NOT NULL
    );
    """


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_audit_db_schema())
    conn.close()


def _make_adapter(db_path: Path, skill_root: Path) -> SkillStoreAdapter:
    return SkillStoreAdapter(
        kms=_InMemoryKms(),
        db_path=db_path,
        skill_store_root=skill_root,
        runtime_version="test",
    )


def _make_skill_md_content(name: str = "test-skill") -> str:
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: A test skill\n"
        f"version: '1'\n"
        f"---\n\n"
        f"## When\n- always\n\n"
        f"## Procedure\n1. do the thing\n\n"
        f"## Pitfalls\n- none\n\n"
        f"## Verification\n- check the thing\n"
    )


def _make_captured_action(
    *,
    action: str = "create",
    name: str = "test-skill",
    content: str | None = None,
    tenant_id: UUID | None = None,
) -> CapturedAction:
    params: dict = {"action": action, "name": name}
    if content is not None:
        params["content"] = content
    elif action in ("create", "edit"):
        params["content"] = _make_skill_md_content(name)
    return CapturedAction(
        surface_kind=SurfaceKind.SKILL_STORE,
        intent_desc=f"nous skill_manage {action}",
        payload=params,
        tenant_id=tenant_id or uuid4(),
        human_operator_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# (a) skill_manage create via broker → SKILL.md written + SIGNED v2
# ---------------------------------------------------------------------------


class TestSkillManageCreateWritesSignedSkill:
    async def test_create_writes_skill_md_on_disk(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="pay-invoice")
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        skill_file = skill_root / "pay-invoice" / "SKILL.md"
        assert skill_file.exists(), "SKILL.md must be written to disk"

    async def test_create_persists_v2_signature_to_skill_md(
        self, tmp_path: Path
    ) -> None:
        """Governance fields are embedded in SKILL.md frontmatter.metadata."""
        import yaml as _yaml

        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="pay-invoice")
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        skill_file = skill_root / "pay-invoice" / "SKILL.md"
        assert skill_file.exists()

        content = skill_file.read_text()
        assert content.startswith("---")
        end = content.find("---", 3)
        fm = _yaml.safe_load(content[3:end]) or {}
        meta = fm.get("metadata") or {}
        assert meta.get("signing_method") == "v2", "must be v2 signature"
        assert meta.get("signature_hex"), "signature_hex must be present"
        assert len(meta["signature_hex"]) == 64, "HMAC-SHA256 = 64 hex chars"
        assert meta.get("state") == "validated", "initial state must be validated"

    async def test_create_never_writes_unsigned_skill(
        self, tmp_path: Path
    ) -> None:
        """Fail-closed: signing always runs; governance written to frontmatter."""
        import yaml as _yaml

        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="unsigned-skill")
        outcome = await adapter.replay(action)
        if outcome.status == ReplayStatus.EXECUTED_OK:
            skill_file = skill_root / "unsigned-skill" / "SKILL.md"
            content = skill_file.read_text()
            end = content.find("---", 3)
            fm = _yaml.safe_load(content[3:end]) or {}
            meta = fm.get("metadata") or {}
            assert meta.get("signing_method") == "v2", "must always be v2"
            assert meta.get("signature_hex") is not None

    async def test_create_with_bad_frontmatter_returns_failed(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(
            action="create",
            name="bad-skill",
            content="no frontmatter here",
        )
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        assert "SKILL.md" in (outcome.error or "")

    async def test_create_with_name_mismatch_returns_failed(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        # parameters.name='wrong-name' but frontmatter.name='test-skill'
        action = _make_captured_action(
            action="create",
            name="wrong-name",
            content=_make_skill_md_content("test-skill"),
        )
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        assert "mismatch" in (outcome.error or "").lower()

    async def test_result_includes_package_id_and_state(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="my-skill")
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert "package_id" in outcome.result
        assert outcome.result["state"] == "validated"
        assert outcome.result["signing_method"] == "v2"


# ---------------------------------------------------------------------------
# (b) Unsigned / v1 / manipulated skill → not promoted (fail-closed)
# ---------------------------------------------------------------------------


class TestFailClosedSignatureVerification:
    """SkillGovernanceService.promote_skill fails on missing/v1/tampered sigs."""

    def _insert_skill(
        self,
        db_path: Path,
        *,
        state: str = "validated",
        signing_method: str = "v2",
        signature_hex: str | None = None,
    ) -> str:
        if signature_hex is None and signing_method == "v2":
            # Compute a valid-looking 64-char hex (won't verify, but has length)
            payload = b"test"
            signature_hex = hmac.new(
                _FAKE_KEY, payload, hashlib.sha256
            ).hexdigest()

        pkg_id = str(uuid4())
        from datetime import UTC, datetime
        signed_at = datetime.now(tz=UTC).isoformat()

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO skill_packages_view (
              package_id, skill_id, skill_name, version,
              state, surface_kinds, signed_at, signature_short,
              signing_method, signature_hex
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pkg_id,
                str(uuid4()),
                "test-skill",
                1,
                state,
                "skill_store",
                signed_at,
                (signature_hex[:12] if signature_hex else None),
                signing_method,
                signature_hex,
            ),
        )
        conn.commit()
        conn.close()
        return pkg_id

    async def test_missing_signature_rejects_promote(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        _init_db(db_path)
        pkg_id = self._insert_skill(
            db_path,
            state="validated",
            signing_method="v1",
            signature_hex=None,
        )
        svc = SkillGovernanceService(db_path=db_path)

        with pytest.raises(SkillSignatureVerificationFailed, match="signing_method"):
            from unittest.mock import patch
            import hermes.shell_server.skills.native_keystore_adapter as _mod
            with patch.object(
                _mod, "SecretsVault", return_value=type("V", (), {"derive_subkey": lambda self, **kw: _FAKE_KEY})()
            ):
                await svc.promote_skill(
                    package_id=pkg_id,
                    promoted_by=uuid4(),
                )

    async def test_v1_signature_rejects_promote(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        _init_db(db_path)
        pkg_id = self._insert_skill(
            db_path,
            state="validated",
            signing_method="v1",
            signature_hex="a" * 64,
        )
        svc = SkillGovernanceService(db_path=db_path)

        with pytest.raises(SkillSignatureVerificationFailed):
            from unittest.mock import patch
            import hermes.shell_server.skills.native_keystore_adapter as _mod
            with patch.object(
                _mod, "SecretsVault", return_value=type("V", (), {"derive_subkey": lambda self, **kw: _FAKE_KEY})()
            ):
                await svc.promote_skill(
                    package_id=pkg_id,
                    promoted_by=uuid4(),
                )

    async def test_short_signature_rejects_promote(
        self, tmp_path: Path
    ) -> None:
        """Signature_hex must be exactly 64 chars for a valid SHA-256 HMAC."""
        db_path = tmp_path / "audit.db"
        _init_db(db_path)
        pkg_id = self._insert_skill(
            db_path,
            state="validated",
            signing_method="v2",
            signature_hex="abc123",  # only 6 chars — invalid
        )
        svc = SkillGovernanceService(db_path=db_path)

        with pytest.raises(SkillSignatureVerificationFailed, match="signature_hex"):
            from unittest.mock import patch
            import hermes.shell_server.skills.native_keystore_adapter as _mod
            with patch.object(
                _mod, "SecretsVault", return_value=type("V", (), {"derive_subkey": lambda self, **kw: _FAKE_KEY})()
            ):
                await svc.promote_skill(
                    package_id=pkg_id,
                    promoted_by=uuid4(),
                )

    async def test_tampered_skill_md_fails_verify_signature(self) -> None:
        """Mutating content_hash after signing invalidates the training-domain signature."""
        kms = _InMemoryKms()
        signer = SkillSigner(kms=kms)

        pkg = SkillPackage(
            package_id=uuid4(),
            skill_id=uuid4(),
            skill_version=1,
            tenant_id=uuid4(),
            replay_script_id=uuid4(),
            voice_narrative_id=uuid4(),
            decision_rule_ids=(),
            state=SkillState.VALIDATED,
            signature_hex="",
            signing_key_id="",
            runtime_version="test",
            compiled_by_operator_id=uuid4(),
            content_hash="a" * 64,
        )
        signed = await signer.sign(package=pkg, signing_key_id=_KEY_ID)

        # Tamper content_hash — simulates SKILL.md file mutation
        tampered = replace(signed, content_hash="b" * 64)
        with pytest.raises(SignatureVerificationError):
            await verify_skill_signature(package=tampered, kms=kms)

    async def test_no_signature_fails_verify(self) -> None:
        kms = _InMemoryKms()
        pkg = SkillPackage(
            package_id=uuid4(),
            skill_id=uuid4(),
            skill_version=1,
            tenant_id=uuid4(),
            replay_script_id=uuid4(),
            voice_narrative_id=uuid4(),
            decision_rule_ids=(),
            state=SkillState.VALIDATED,
            signature_hex="",
            signing_key_id="",
            runtime_version="test",
            compiled_by_operator_id=uuid4(),
            content_hash="a" * 64,
        )
        with pytest.raises(SignatureVerificationError):
            await verify_skill_signature(package=pkg, kms=kms)


# ---------------------------------------------------------------------------
# (d) Both paths go to the same store + same governance gate
# ---------------------------------------------------------------------------


class TestBothPathsUseUnifiedStore:
    async def test_autonomous_path_writes_skill_md_to_native_dir(
        self, tmp_path: Path
    ) -> None:
        """SkillStoreAdapter writes to the native skills dir (not only the DB)."""
        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="auto-skill")
        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        # The SKILL.md must exist on disk in the native skills dir
        skill_file = skill_root / "auto-skill" / "SKILL.md"
        assert skill_file.exists(), "SKILL.md must be written to the native dir"

    async def test_native_list_reflects_autonomous_skill_with_governance(
        self, tmp_path: Path
    ) -> None:
        """list_skills_native() includes cage-signed skills with their governance."""
        import yaml as _yaml
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _list_native_skills_primary,
        )

        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="listed-skill")
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        # Enumerate via the native scanner pointing at our test skill_root
        skills = _list_native_skills_primary(skills_root=skill_root)
        skill = next((s for s in skills if s["skill_name"] == "listed-skill"), None)
        assert skill is not None, "cage-created skill must appear in native list"
        assert skill["signing_method"] == "v2"
        assert skill["state"] == "validated"

    async def test_delete_action_removes_skill_from_disk(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        create_action = _make_captured_action(action="create", name="delete-me")
        outcome = await adapter.replay(create_action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        skill_file = skill_root / "delete-me" / "SKILL.md"
        assert skill_file.exists()

        delete_action = _make_captured_action(action="delete", name="delete-me")
        del_outcome = await adapter.replay(delete_action)
        assert del_outcome.status == ReplayStatus.EXECUTED_OK
        assert not skill_file.exists(), "SKILL.md must be removed on delete"

    async def test_skill_manage_skill_not_autonomous_until_promoted(
        self, tmp_path: Path
    ) -> None:
        """Autonomous path starts at validated, never at autonomous (constitución)."""
        import yaml as _yaml

        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="needs-promote")
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        skill_file = skill_root / "needs-promote" / "SKILL.md"
        content = skill_file.read_text()
        end = content.find("---", 3)
        fm = _yaml.safe_load(content[3:end]) or {}
        meta = fm.get("metadata") or {}

        # Must be validated, NOT autonomous — requires promote to advance
        assert meta.get("state") == "validated"
        assert meta.get("state") != "autonomous"


# ---------------------------------------------------------------------------
# (e) Progressive loading works for signed skills
# ---------------------------------------------------------------------------


class TestProgressiveLoadingSignedSkills:
    """list_skills_native() returns signed skills; unsigned skills never enter the store."""

    async def test_list_skills_returns_signed_skill(
        self, tmp_path: Path
    ) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _list_native_skills_primary,
        )

        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        action = _make_captured_action(action="create", name="loadable-skill")
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        skills = _list_native_skills_primary(skills_root=skill_root)
        assert len(skills) == 1
        assert skills[0]["skill_name"] == "loadable-skill"

    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _list_native_skills_primary,
        )

        empty_root = tmp_path / "skills"
        skills = _list_native_skills_primary(skills_root=empty_root)
        assert skills == []

    async def test_deleted_skill_not_in_native_list(
        self, tmp_path: Path
    ) -> None:
        """Deleted skills are removed from disk and do not appear in native list."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _list_native_skills_primary,
        )

        db_path = tmp_path / "audit.db"
        skill_root = tmp_path / "skills"
        _init_db(db_path)
        adapter = _make_adapter(db_path, skill_root)

        create_action = _make_captured_action(action="create", name="gone-skill")
        await adapter.replay(create_action)

        delete_action = _make_captured_action(action="delete", name="gone-skill")
        await adapter.replay(delete_action)

        skills = _list_native_skills_primary(skills_root=skill_root)
        names = [s["skill_name"] for s in skills]
        assert "gone-skill" not in names, "deleted skill must not appear in native list"


# ---------------------------------------------------------------------------
# SkillMdDocument unit tests — parse/serialize invariants
# ---------------------------------------------------------------------------


class TestSkillMdDocumentParseSerialize:
    def test_roundtrip(self) -> None:
        original = _make_skill_md_content("my-skill")
        doc = parse_skill_md(original)
        reserialized = serialize_skill_md(doc)
        reparsed = parse_skill_md(reserialized)
        assert reparsed.name == doc.name
        assert reparsed.description == doc.description
        assert reparsed.version == doc.version
        assert reparsed.body.strip() == doc.body.strip()

    def test_missing_name_raises(self) -> None:
        content = "---\ndescription: test\nversion: '1'\n---\n\nbody\n"
        with pytest.raises(SkillMdParseError, match="name"):
            parse_skill_md(content)

    def test_missing_description_raises(self) -> None:
        content = "---\nname: test-skill\nversion: '1'\n---\n\nbody\n"
        with pytest.raises(SkillMdParseError, match="description"):
            parse_skill_md(content)

    def test_missing_version_raises(self) -> None:
        content = "---\nname: test-skill\ndescription: test\n---\n\nbody\n"
        with pytest.raises(SkillMdParseError, match="version"):
            parse_skill_md(content)

    def test_empty_body_raises(self) -> None:
        content = "---\nname: test-skill\ndescription: test\nversion: '1'\n---\n"
        with pytest.raises(SkillMdParseError):
            parse_skill_md(content)

    def test_no_frontmatter_raises(self) -> None:
        with pytest.raises(SkillMdParseError):
            parse_skill_md("just plain text")

    def test_invalid_name_raises(self) -> None:
        with pytest.raises(SkillMdParseError, match="Invalid skill name"):
            SkillMdDocument(
                name="INVALID NAME!",
                description="test",
                version="1",
                body="body",
            )

    def test_content_bytes_is_deterministic(self) -> None:
        doc = parse_skill_md(_make_skill_md_content("det-skill"))
        assert skill_md_content_bytes(doc) == skill_md_content_bytes(doc)

    def test_different_content_produces_different_hash(self) -> None:
        doc_a = parse_skill_md(_make_skill_md_content("skill-a"))
        doc_b = parse_skill_md(_make_skill_md_content("skill-b"))
        hash_a = hashlib.sha256(skill_md_content_bytes(doc_a)).hexdigest()
        hash_b = hashlib.sha256(skill_md_content_bytes(doc_b)).hexdigest()
        assert hash_a != hash_b
