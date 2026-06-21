"""P0-4 (hardened): Skill signing key resolution tests.

Security hardening (red-team remediation):
  - resolve_signing_key is FAIL-CLOSED: raises SigningKeyError when master.key
    is absent. No v1 fallback for signing — v1 keys are publicly derivable
    from the DB path (CWE-321) and must never be used for new signatures.
  - NativeKeyStoreAdapter raises SigningKeyError when master.key absent.
  - NativeKeyStoreAdapter.get_signing_key_sync() works when vault is available.
  - persist_composio_skill writes signing_method='v2' column to DB.
  - signing_method='v2' when NativeKeyStoreAdapter succeeds.
  - Existing skills with signing_method='v1' remain readable from DB
    (for audit/display only — cannot be promoted to AUTONOMOUS).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes.shell_server.audit_api import init_schema
from hermes.shell_server.skills.composio_skill_service import persist_composio_skill
from hermes.shell_server.training.persist import build_signing_key, resolve_signing_key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# resolve_signing_key — fallback behaviour
# ---------------------------------------------------------------------------


class TestResolveSigningKey:
    def test_raises_when_master_key_absent(self, tmp_path: Path) -> None:
        """resolve_signing_key RAISES SigningKeyError when master.key is absent.

        Security regression: the old v1 fallback (sha256(db_path)) is publicly
        derivable and must not be used for signing. Absent master.key is a fatal
        misconfiguration, not a graceful degradation.
        """
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415

        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no master.key")):
            with pytest.raises(SigningKeyError):
                resolve_signing_key(tmp_path / "shell-state.db")

    def test_raises_not_falls_back_to_v1_when_native_unavailable(self, tmp_path: Path) -> None:
        """Explicit regression: v1 fallback must never be returned for signing.

        The old code returned (build_signing_key(db), 'v1') when SecretsVault
        raised. That is now a security violation — SigningKeyError must be raised.
        """
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415

        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        db = tmp_path / "shell-state.db"
        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no key")):
            with pytest.raises(SigningKeyError):
                resolve_signing_key(db)

    def test_uses_v2_when_native_keystore_available(self, tmp_path: Path) -> None:
        db = tmp_path / "shell-state.db"
        fake_key = b"\xAB" * 32

        class _FakeVault:
            def derive_subkey(self, *, label: str) -> bytes:
                return fake_key

        with patch(
            "hermes.shell_server.skills.native_keystore_adapter.SecretsVault",
            return_value=_FakeVault(),
        ):
            key, method = resolve_signing_key(db)

        assert method == "v2"
        assert key == fake_key

    def test_v2_key_differs_from_v1_key(self, tmp_path: Path) -> None:
        """v2 (native) key must not equal v1 (path-HMAC) key — different derivation."""
        db = tmp_path / "shell-state.db"
        fake_key = b"\xCD" * 32

        class _FakeVault:
            def derive_subkey(self, *, label: str) -> bytes:
                return fake_key

        with patch(
            "hermes.shell_server.skills.native_keystore_adapter.SecretsVault",
            return_value=_FakeVault(),
        ):
            v2_key, _ = resolve_signing_key(db)

        v1_key = build_signing_key(db)
        assert v2_key != v1_key


# ---------------------------------------------------------------------------
# NativeKeyStoreAdapter — absent master.key
# ---------------------------------------------------------------------------


class TestNativeKeyStoreAdapterAbsent:
    def test_raises_signing_key_error_when_master_key_absent(self) -> None:
        """NativeKeyStoreAdapter constructor must raise SigningKeyError on RuntimeError."""
        import importlib  # noqa: PLC0415
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415

        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no key")):
            with pytest.raises(SigningKeyError):
                _mod.NativeKeyStoreAdapter()

    def test_get_signing_key_sync_returns_bytes(self) -> None:
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        fake_key = b"\x12" * 32

        class _FakeVault:
            def derive_subkey(self, *, label: str) -> bytes:
                return fake_key

        with patch.object(_mod, "SecretsVault", return_value=_FakeVault()):
            adapter = _mod.NativeKeyStoreAdapter()
            result = adapter.get_signing_key_sync()
        assert result == fake_key


# ---------------------------------------------------------------------------
# persist_composio_skill — signing_method persisted to DB
# ---------------------------------------------------------------------------


class TestComposioSkillSigningMethod:
    def test_signing_method_column_written(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        init_schema(db)

        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        fake_key = b"\xAA" * 32

        class _FakeVault:
            def derive_subkey(self, *, label: str) -> bytes:
                return fake_key

        with patch.object(_mod, "SecretsVault", return_value=_FakeVault()):
            result = persist_composio_skill(
                db_path=db,
                skill_name="test-skill",
                toolkit_slug="SLACK",
                intent_text="Send a notification",
                signed_at="2026-06-03T00:00:00+00:00",
            )
        assert "signing_method" in result
        assert result["signing_method"] == "v2"

    def test_raises_when_native_unavailable(self, tmp_path: Path) -> None:
        """persist_composio_skill RAISES when master.key absent — no v1 fallback."""
        db = tmp_path / "test.db"
        init_schema(db)

        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415

        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no key")):
            with pytest.raises(SigningKeyError):
                persist_composio_skill(
                    db_path=db,
                    skill_name="gmail-skill",
                    toolkit_slug="GMAIL",
                    intent_text="Send an email",
                    signed_at="2026-06-03T00:00:00+00:00",
                )

    def test_v2_signing_method_when_native_available(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        init_schema(db)

        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        fake_key = b"\xFF" * 32

        class _FakeVault:
            def derive_subkey(self, *, label: str) -> bytes:
                return fake_key

        with patch.object(_mod, "SecretsVault", return_value=_FakeVault()):
            result = persist_composio_skill(
                db_path=db,
                skill_name="slack-skill",
                toolkit_slug="SLACK",
                intent_text="Post to Slack",
                signed_at="2026-06-03T00:00:00+00:00",
            )

        assert result["signing_method"] == "v2"

    def test_v1_and_v2_skills_coexist_in_db(self, tmp_path: Path) -> None:
        """Existing v1 skills remain readable alongside new v2 skills."""
        db = tmp_path / "test.db"
        init_schema(db)

        # Insert a legacy v1 skill directly (simulates pre-migration row).
        from uuid import uuid4  # noqa: PLC0415
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        pkg_v1 = str(uuid4())
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            INSERT INTO skill_packages_view
              (package_id, skill_id, skill_name, version, state, surface_kinds,
               signed_at, signature_short, signing_method)
            VALUES (?, 'old-skill', 'old-skill', 1, 'validated', 'browser',
                    '2026-01-01T00:00:00+00:00', 'aabbcc112233', 'v1')
            """,
            (pkg_v1,),
        )
        conn.commit()
        conn.close()

        # Create a new v2 skill.
        fake_key = b"\xEE" * 32

        class _FakeVault:
            def derive_subkey(self, *, label: str) -> bytes:
                return fake_key

        with patch.object(_mod, "SecretsVault", return_value=_FakeVault()):
            r_v2 = persist_composio_skill(
                db_path=db,
                skill_name="new-skill",
                toolkit_slug="GITHUB",
                intent_text="Open a PR",
                signed_at="2026-06-03T00:00:00+00:00",
            )

        # Both rows exist.
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT package_id, signing_method FROM skill_packages_view ORDER BY signed_at"
        ).fetchall()
        conn.close()

        methods = {row[0]: row[1] for row in rows}
        assert methods[pkg_v1] == "v1"
        assert methods[r_v2["package_id"]] == "v2"


# ---------------------------------------------------------------------------
# Migration safety: signing_method column is added to existing DBs
# ---------------------------------------------------------------------------


class TestSigningMethodMigration:
    def test_migration_adds_column_to_existing_db(self, tmp_path: Path) -> None:
        """init_schema() must add signing_method to a DB without it."""
        db = tmp_path / "legacy.db"
        # Create old schema WITHOUT signing_method.
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS skill_packages_view (
              package_id TEXT PRIMARY KEY,
              skill_id TEXT NOT NULL,
              skill_name TEXT NOT NULL,
              version INTEGER NOT NULL,
              state TEXT NOT NULL,
              surface_kinds TEXT NOT NULL,
              signed_at TEXT NOT NULL,
              signature_short TEXT
            );
            """
        )
        from uuid import uuid4 as _uuid4  # noqa: PLC0415
        conn.execute(
            "INSERT INTO skill_packages_view VALUES (?,?,?,1,'validated','browser','2026-01-01',NULL)",
            (str(_uuid4()), "s", "s"),
        )
        conn.commit()
        conn.close()

        # Running init_schema must add the column (migration).
        init_schema(db)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT signing_method FROM skill_packages_view"
        ).fetchone()
        conn.close()
        # Default 'v1' is applied to existing rows.
        assert row is not None
        assert row[0] == "v1"
