"""Tests for the Composio skill creation path.

Covers:
  - persist_composio_skill: persists validated SkillPackage with
    surface_kinds={API_CALL} + toolkit/intent; version increments; signature
    present; control-char intent rejected; empty fields rejected.
  - verify_toolkit_connected: toolkit not connected → ComposioToolkitNotConnected;
    no credential → ComposioCredentialMissing.
  - HTTP route: happy path → 201 + DTO; bad input → 400; not connected → 400;
    appears in GET /skills with skill_kind="composio" + toolkit_slug.
  - Schema: fresh DB has composio_skills table; idempotent re-run.
  - get_composio_skill_detail: retrieves stored toolkit_slug + intent_text.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fake vault — mirrors test_skill_promote.py / test_training_capture.py.
# Provides a stable 32-byte key so the v2 HMAC path succeeds without
# needing /var/lib/hermes/master.key (fail-closed in production, bypassed
# in tests via patch).
# ---------------------------------------------------------------------------

_FAKE_SIGNING_KEY = b"\xAA" * 32


class _FakeVault:
    """SecretsVault stand-in that returns a fixed key for test isolation."""

    def derive_subkey(self, *, label: str) -> bytes:  # noqa: ARG002
        return _FAKE_SIGNING_KEY


def _fake_vault_patch():
    """Return a context manager that patches SecretsVault in native_keystore_adapter."""
    import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

    return patch.object(_mod, "SecretsVault", return_value=_FakeVault())

from hermes.shell_server.audit_api import create_audit_router
from hermes.shell_server.skills.composio_skill_errors import (
    ComposioCredentialMissing,
    ComposioSkillNameConflict,
    ComposioSkillValidationError,
    ComposioToolkitNotConnected,
)
from hermes.shell_server.skills.composio_skill_service import (
    get_composio_skill_detail,
    persist_composio_skill,
    verify_toolkit_connected,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeConnectedAccount:
    id: str
    toolkit_slug: str
    entity_id: str
    status: str


def _make_client(db_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_audit_router(db_path))
    return TestClient(app)


def _assert_composio_skills_table_exists(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='composio_skills'"
    ).fetchone()
    conn.close()
    assert row is not None, "composio_skills table must exist"


# ---------------------------------------------------------------------------
# persist_composio_skill — unit tests (no HTTP, no Composio network)
# ---------------------------------------------------------------------------


class TestPersistComposioSkill:
    def test_persists_validated_skill_with_api_call_surface(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "test.db"
        _make_client(db)  # init schema

        signed_at = "2026-06-01T00:00:00+00:00"
        with _fake_vault_patch():
            result = persist_composio_skill(
                db_path=db,
                skill_name="send-gmail",
                toolkit_slug="GMAIL",
                intent_text="Send a follow-up email to the prospect",
                signed_at=signed_at,
            )

        assert result["state"] == "validated"
        assert result["surface_kinds"] == ["api_call"]
        assert result["skill_kind"] == "composio"
        assert result["toolkit_slug"] == "GMAIL"
        assert result["signature_short"] is not None
        assert len(result["signature_short"]) == 12
        assert result["validated_at"] == signed_at
        assert result["promoted_at"] is None
        # v2 signing asserts — fail-closed produces a full 64-char HMAC hex.
        assert result["signing_method"] == "v2"

    def test_signature_is_present_and_non_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with _fake_vault_patch():
            result = persist_composio_skill(
                db_path=db,
                skill_name="check-calendar",
                toolkit_slug="GOOGLECALENDAR",
                intent_text="Check today's meetings",
                signed_at="2026-06-01T10:00:00+00:00",
            )
        assert result["signature_short"]
        assert result["signature_short"] != ""
        # v2: full HMAC-SHA256 hex is 64 chars; short is its prefix.
        assert result["signing_method"] == "v2"

    def test_version_increments_monotonically(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with _fake_vault_patch():
            r1 = persist_composio_skill(
                db_path=db,
                skill_name="slack-notify",
                toolkit_slug="SLACK",
                intent_text="Send a Slack message",
                signed_at="2026-06-01T00:00:00+00:00",
            )
            r2 = persist_composio_skill(
                db_path=db,
                skill_name="slack-notify",
                toolkit_slug="SLACK",
                intent_text="Send a Slack message — v2",
                signed_at="2026-06-01T01:00:00+00:00",
            )

        assert r1["version"] == 1
        assert r2["version"] == 2
        assert r1["package_id"] != r2["package_id"]

    def test_empty_skill_name_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with pytest.raises(ComposioSkillValidationError, match="skill_name"):
            persist_composio_skill(
                db_path=db,
                skill_name="",
                toolkit_slug="GMAIL",
                intent_text="Do something",
                signed_at="2026-06-01T00:00:00+00:00",
            )

    def test_empty_toolkit_slug_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with pytest.raises(ComposioSkillValidationError, match="toolkit_slug"):
            persist_composio_skill(
                db_path=db,
                skill_name="my-skill",
                toolkit_slug="",
                intent_text="Do something",
                signed_at="2026-06-01T00:00:00+00:00",
            )

    def test_empty_intent_text_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with pytest.raises(ComposioSkillValidationError, match="intent_text"):
            persist_composio_skill(
                db_path=db,
                skill_name="my-skill",
                toolkit_slug="GMAIL",
                intent_text="",
                signed_at="2026-06-01T00:00:00+00:00",
            )

    def test_whitespace_only_intent_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with pytest.raises(ComposioSkillValidationError, match="intent_text"):
            persist_composio_skill(
                db_path=db,
                skill_name="my-skill",
                toolkit_slug="GMAIL",
                intent_text="   \t  ",
                signed_at="2026-06-01T00:00:00+00:00",
            )

    def test_control_chars_in_intent_rejected(self, tmp_path: Path) -> None:
        """Intent text with ASCII control chars (e.g. NUL, BEL) must be rejected."""
        db = tmp_path / "test.db"
        _make_client(db)

        with pytest.raises(ComposioSkillValidationError, match="control character"):
            persist_composio_skill(
                db_path=db,
                skill_name="my-skill",
                toolkit_slug="GMAIL",
                intent_text="Do \x00something\x07bad",
                signed_at="2026-06-01T00:00:00+00:00",
            )

    def test_newlines_in_intent_allowed(self, tmp_path: Path) -> None:
        """Newline and tab are valid whitespace and must NOT be rejected."""
        db = tmp_path / "test.db"
        _make_client(db)

        with _fake_vault_patch():
            result = persist_composio_skill(
                db_path=db,
                skill_name="my-skill",
                toolkit_slug="GMAIL",
                intent_text="Step 1: open email\nStep 2: send reply\t(urgent)",
                signed_at="2026-06-01T00:00:00+00:00",
            )
        assert result["state"] == "validated"

    def test_intent_over_max_length_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with pytest.raises(ComposioSkillValidationError, match="intent_text"):
            persist_composio_skill(
                db_path=db,
                skill_name="my-skill",
                toolkit_slug="GMAIL",
                intent_text="x" * 2001,
                signed_at="2026-06-01T00:00:00+00:00",
            )

    def test_skill_stored_in_composio_skills_table(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with _fake_vault_patch():
            result = persist_composio_skill(
                db_path=db,
                skill_name="github-pr",
                toolkit_slug="GITHUB",
                intent_text="Open a PR with the changes",
                signed_at="2026-06-01T00:00:00+00:00",
            )

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT * FROM composio_skills WHERE package_id = ?",
            (result["package_id"],),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[1] == "GITHUB"  # toolkit_slug
        assert row[2] == "Open a PR with the changes"  # intent_text


# ---------------------------------------------------------------------------
# get_composio_skill_detail — retrieval accessor
# ---------------------------------------------------------------------------


class TestGetComposioSkillDetail:
    def test_returns_detail_for_existing_skill(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with _fake_vault_patch():
            result = persist_composio_skill(
                db_path=db,
                skill_name="gdrive-upload",
                toolkit_slug="GOOGLEDRIVE",
                intent_text="Upload the weekly report to Drive",
                signed_at="2026-06-01T00:00:00+00:00",
            )
        detail = get_composio_skill_detail(db_path=db, package_id=result["package_id"])

        assert detail is not None
        assert detail["toolkit_slug"] == "GOOGLEDRIVE"
        assert detail["intent_text"] == "Upload the weekly report to Drive"
        assert detail["package_id"] == result["package_id"]

    def test_returns_none_for_missing_package(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        result = get_composio_skill_detail(db_path=db, package_id=str(uuid4()))
        assert result is None


# ---------------------------------------------------------------------------
# verify_toolkit_connected — fake ComposioClient
# ---------------------------------------------------------------------------


class TestVerifyToolkitConnected:
    """Tests patch at the real import sites (lazy-imported inside async fn)."""

    @pytest.mark.asyncio
    async def test_raises_when_toolkit_not_connected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"

        fake_account = FakeConnectedAccount(
            id="acc-1",
            toolkit_slug="SLACK",
            entity_id="default",
            status="ACTIVE",
        )

        with (
            patch(
                "hermes.shell_server.integrations.repo.SQLiteIntegrationsRepository"
            ) as MockRepo,
            patch("hermes.shell_server.security.secrets.SecretsVault"),
            patch(
                "hermes.integrations.composio.composio_client.ComposioClient"
            ) as MockClient,
        ):
            mock_repo = MockRepo.return_value
            mock_repo.reveal_api_key.return_value = "fake-key"
            mock_repo.get_or_none.return_value = None

            mock_client = MockClient.return_value
            mock_client.list_connected_accounts = AsyncMock(
                return_value=[fake_account]
            )

            with pytest.raises(ComposioToolkitNotConnected, match="GMAIL"):
                await verify_toolkit_connected(
                    db_path=db,
                    toolkit_slug="GMAIL",
                )

    @pytest.mark.asyncio
    async def test_passes_when_toolkit_is_connected(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"

        fake_account = FakeConnectedAccount(
            id="acc-1",
            toolkit_slug="GMAIL",
            entity_id="default",
            status="ACTIVE",
        )

        with (
            patch(
                "hermes.shell_server.integrations.repo.SQLiteIntegrationsRepository"
            ) as MockRepo,
            patch("hermes.shell_server.security.secrets.SecretsVault"),
            patch(
                "hermes.integrations.composio.composio_client.ComposioClient"
            ) as MockClient,
        ):
            mock_repo = MockRepo.return_value
            mock_repo.reveal_api_key.return_value = "fake-key"
            mock_repo.get_or_none.return_value = None

            mock_client = MockClient.return_value
            mock_client.list_connected_accounts = AsyncMock(
                return_value=[fake_account]
            )

            # Must NOT raise.
            await verify_toolkit_connected(
                db_path=db,
                toolkit_slug="GMAIL",
            )

    @pytest.mark.asyncio
    async def test_raises_when_no_credential(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"

        from hermes.shell_server.integrations.domain import IntegrationNotFound  # noqa: PLC0415

        with (
            patch(
                "hermes.shell_server.integrations.repo.SQLiteIntegrationsRepository"
            ) as MockRepo,
            patch("hermes.shell_server.security.secrets.SecretsVault"),
        ):
            mock_repo = MockRepo.return_value
            mock_repo.reveal_api_key.side_effect = IntegrationNotFound("composio")

            with pytest.raises(ComposioCredentialMissing):
                await verify_toolkit_connected(
                    db_path=db,
                    toolkit_slug="GMAIL",
                )

    @pytest.mark.asyncio
    async def test_raises_when_api_key_is_none(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"

        with (
            patch(
                "hermes.shell_server.integrations.repo.SQLiteIntegrationsRepository"
            ) as MockRepo,
            patch("hermes.shell_server.security.secrets.SecretsVault"),
        ):
            mock_repo = MockRepo.return_value
            mock_repo.reveal_api_key.return_value = None

            with pytest.raises(ComposioCredentialMissing):
                await verify_toolkit_connected(
                    db_path=db,
                    toolkit_slug="GMAIL",
                )

    @pytest.mark.asyncio
    async def test_slug_comparison_is_case_insensitive(self, tmp_path: Path) -> None:
        """Toolkit slugs may be lowercase in user input but uppercase in Composio."""
        db = tmp_path / "test.db"

        fake_account = FakeConnectedAccount(
            id="acc-1",
            toolkit_slug="GMAIL",
            entity_id="default",
            status="ACTIVE",
        )

        with (
            patch(
                "hermes.shell_server.integrations.repo.SQLiteIntegrationsRepository"
            ) as MockRepo,
            patch("hermes.shell_server.security.secrets.SecretsVault"),
            patch(
                "hermes.integrations.composio.composio_client.ComposioClient"
            ) as MockClient,
        ):
            mock_repo = MockRepo.return_value
            mock_repo.reveal_api_key.return_value = "fake-key"
            mock_repo.get_or_none.return_value = None

            mock_client = MockClient.return_value
            mock_client.list_connected_accounts = AsyncMock(
                return_value=[fake_account]
            )

            # lowercase slug in request, uppercase returned by Composio — must pass.
            await verify_toolkit_connected(
                db_path=db,
                toolkit_slug="gmail",
            )


# ---------------------------------------------------------------------------
# HTTP route: POST /api/v1/skills/composio
# ---------------------------------------------------------------------------


def _patch_verify_connected_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch verify_toolkit_connected to succeed (no Composio network call)."""
    monkeypatch.setattr(
        "hermes.shell_server.audit_api.create_composio_skill.__code__",
        None,  # replaced below
    )


class TestComposioSkillRoute:
    @pytest.fixture(autouse=True)
    def _patch_vault(self):
        """Activate the fake vault for every test in this class.

        The route handler calls persist_composio_skill → _resolve_signing_key →
        NativeKeyStoreAdapter → SecretsVault during HTTP request processing, so
        the patch must be active for the duration of each test, not only at
        fixture construction time.
        """
        with _fake_vault_patch():
            yield

    @pytest.fixture
    def connected_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        """Client where verify_toolkit_connected is always a no-op."""
        app = FastAPI()
        app.include_router(create_audit_router(tmp_path / "test.db"))

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            "hermes.shell_server.skills.composio_skill_service.verify_toolkit_connected",
            _noop,
        )
        return TestClient(app)

    @pytest.fixture
    def not_connected_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        """Client where verify_toolkit_connected raises ComposioToolkitNotConnected."""
        app = FastAPI()
        app.include_router(create_audit_router(tmp_path / "test.db"))

        async def _raise(*args: Any, **kwargs: Any) -> None:
            raise ComposioToolkitNotConnected("GMAIL")

        monkeypatch.setattr(
            "hermes.shell_server.skills.composio_skill_service.verify_toolkit_connected",
            _raise,
        )
        return TestClient(app)

    def test_happy_path_returns_201_with_validated_dto(
        self, connected_client: TestClient
    ) -> None:
        r = connected_client.post(
            "/api/v1/skills/composio",
            json={
                "skill_name": "send-invoice-email",
                "toolkit_slug": "GMAIL",
                "intent_text": "Send the invoice email to the client",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["state"] == "validated"
        assert body["surface_kinds"] == ["api_call"]
        assert body["skill_kind"] == "composio"
        assert body["toolkit_slug"] == "GMAIL"
        assert body["signature_short"] is not None
        assert body["validated_at"] is not None
        assert body["promoted_at"] is None

    def test_skill_appears_in_list_with_kind_and_slug(
        self, connected_client: TestClient
    ) -> None:
        connected_client.post(
            "/api/v1/skills/composio",
            json={
                "skill_name": "slack-notify",
                "toolkit_slug": "SLACK",
                "intent_text": "Post a notification to #ops",
            },
        )

        r = connected_client.get("/api/v1/skills")
        assert r.status_code == 200
        items = r.json()
        composio_items = [i for i in items if i.get("skill_kind") == "composio"]
        assert len(composio_items) == 1
        assert composio_items[0]["toolkit_slug"] == "SLACK"

    def test_empty_skill_name_returns_422(
        self, connected_client: TestClient
    ) -> None:
        r = connected_client.post(
            "/api/v1/skills/composio",
            json={
                "skill_name": "",
                "toolkit_slug": "GMAIL",
                "intent_text": "Do something",
            },
        )
        # FastAPI validates Field(min_length=1) → 422.
        assert r.status_code == 422

    def test_missing_toolkit_slug_returns_422(
        self, connected_client: TestClient
    ) -> None:
        r = connected_client.post(
            "/api/v1/skills/composio",
            json={
                "skill_name": "my-skill",
                "intent_text": "Do something",
            },
        )
        assert r.status_code == 422

    def test_toolkit_not_connected_returns_400(
        self, not_connected_client: TestClient
    ) -> None:
        r = not_connected_client.post(
            "/api/v1/skills/composio",
            json={
                "skill_name": "send-email",
                "toolkit_slug": "GMAIL",
                "intent_text": "Send an email",
            },
        )
        assert r.status_code == 400
        assert "toolkit_not_connected" in r.json()["detail"]

    def test_version_conflict_returns_409(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A duplicate package_id triggers a sqlite3.IntegrityError → 409."""
        db = tmp_path / "test.db"
        app = FastAPI()
        app.include_router(create_audit_router(db))

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            "hermes.shell_server.skills.composio_skill_service.verify_toolkit_connected",
            _noop,
        )
        client = TestClient(app)

        # Patch uuid4 to return the same package_id twice, forcing the PK conflict.
        fixed_id = str(uuid4())

        with patch(
            "hermes.shell_server.skills.composio_skill_service.uuid4",
            return_value=type("FakeUUID", (), {"__str__": lambda self: fixed_id})(),
        ):
            client.post(
                "/api/v1/skills/composio",
                json={
                    "skill_name": "dup-skill",
                    "toolkit_slug": "SLACK",
                    "intent_text": "First creation",
                },
            )
            r2 = client.post(
                "/api/v1/skills/composio",
                json={
                    "skill_name": "dup-skill",
                    "toolkit_slug": "SLACK",
                    "intent_text": "Second creation — same package_id",
                },
            )

        assert r2.status_code == 409

    def test_recorded_skills_in_list_have_kind_recorded(
        self, tmp_path: Path
    ) -> None:
        """Existing recorded skills in the list must have skill_kind='recorded'."""
        db = tmp_path / "test.db"
        app = FastAPI()
        app.include_router(create_audit_router(db))
        client = TestClient(app)

        # Insert a recorded skill directly into DB.
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            INSERT INTO skill_packages_view
              (package_id, skill_id, skill_name, version, state, surface_kinds, signed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), "pay-invoice", "pay-invoice", 1, "validated",
             "browser", "2026-06-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        r = client.get("/api/v1/skills")
        assert r.status_code == 200
        items = r.json()
        recorded = [i for i in items if i.get("skill_kind") == "recorded"]
        assert len(recorded) == 1
        assert recorded[0]["toolkit_slug"] is None


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestComposioSkillsSchema:
    def test_fresh_db_has_composio_skills_table(self, tmp_path: Path) -> None:
        db = tmp_path / "audit.db"
        _make_client(db)  # init_schema() creates the table
        _assert_composio_skills_table_exists(db)

    def test_idempotent_schema_init(self, tmp_path: Path) -> None:
        """Calling create_audit_router twice (re-init) must not fail."""
        db = tmp_path / "audit.db"
        _make_client(db)
        _make_client(db)  # second call — IF NOT EXISTS protects
        _assert_composio_skills_table_exists(db)

    def test_composio_skill_written_to_both_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _make_client(db)

        with _fake_vault_patch():
            result = persist_composio_skill(
                db_path=db,
                skill_name="jira-ticket",
                toolkit_slug="JIRA",
                intent_text="Create a Jira ticket for the bug",
                signed_at="2026-06-01T00:00:00+00:00",
            )

        conn = sqlite3.connect(str(db))
        spv = conn.execute(
            "SELECT * FROM skill_packages_view WHERE package_id = ?",
            (result["package_id"],),
        ).fetchone()
        cs = conn.execute(
            "SELECT * FROM composio_skills WHERE package_id = ?",
            (result["package_id"],),
        ).fetchone()
        conn.close()

        assert spv is not None
        assert cs is not None
        # v2 signing: full hex stored; short is the first 12 chars.
        assert result["signing_method"] == "v2"
        assert len(result["signature_short"]) == 12
