"""POST/GET /api/v1/system/requests — the closed-vocabulary marker (T006,
contracts/install-request.md).

`_INSTANCE_DIR` is monkeypatched to an isolated tmp dir for every test —
never touches the real /var/lib/hermes/instance/. The three invariants
tasks.md calls out by name: an unknown verb/slug is rejected with a 400
and writes NOTHING; a second request for a live verb is idempotent (409,
the existing one); a request past its per-verb TTL is treated as if it
never existed (and is cleaned up on read).
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server import install_requests as ir

pytestmark = pytest.mark.unit

_TOKEN = "test-bearer-token"  # noqa: S105 - test fixture, not a real credential


@pytest.fixture(autouse=True)
def _isolated_instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    instance_dir = tmp_path / "instance"
    monkeypatch.setattr(ir, "_INSTANCE_DIR", instance_dir)
    return instance_dir


def _client() -> TestClient:
    app = FastAPI()
    app.state.shell_webui_token = _TOKEN
    app.include_router(ir.create_install_requests_router())
    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _post(verb: str, **extra: object) -> httpx.Response:
    return _client().post(
        "/api/v1/system/requests", json={"verb": verb, **extra}, headers=_auth_headers()
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_post_without_bearer_is_401(self) -> None:
        r = _client().post("/api/v1/system/requests", json={"verb": "update_system"})
        assert r.status_code == 401

    def test_get_without_bearer_is_401(self) -> None:
        r = _client().get("/api/v1/system/requests")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Closed vocabulary — unknown verb/slug rejected, nothing written
# ---------------------------------------------------------------------------


class TestClosedVocabulary:
    def test_unknown_verb_is_400_and_writes_nothing(self, _isolated_instance_dir: Path) -> None:
        r = _post("delete_everything")
        assert r.status_code == 400
        assert r.json() == {"accepted": False, "code": "unknown_verb"}
        assert not _isolated_instance_dir.exists() or not any(_isolated_instance_dir.iterdir())

    def test_missing_verb_is_400(self) -> None:
        r = _client().post("/api/v1/system/requests", json={}, headers=_auth_headers())
        assert r.status_code == 400
        assert r.json()["code"] == "unknown_verb"

    def test_unknown_slug_is_400_and_writes_nothing(self, _isolated_instance_dir: Path) -> None:
        r = _post("install_companion", slug="totally-not-a-companion")
        assert r.status_code == 400
        assert r.json() == {"accepted": False, "code": "unknown_slug"}
        assert not _isolated_instance_dir.exists() or not any(_isolated_instance_dir.iterdir())

    def test_default_slug_is_applied_when_omitted(self) -> None:
        r = _post("install_companion")
        assert r.status_code == 200
        marker = json.loads((ir._marker_path("install_companion")).read_text())
        assert marker["slug"] == "safent-ads"

    def test_verb_not_a_string_is_400(self) -> None:
        r = _client().post("/api/v1/system/requests", json={"verb": 12345}, headers=_auth_headers())
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Successful creation
# ---------------------------------------------------------------------------


class TestCreation:
    def test_valid_verb_is_accepted_with_a_pending_status(self) -> None:
        r = _post("update_system")
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["request"]["verb"] == "update_system"
        assert body["request"]["state"] == "pending"
        assert "expires_at" in body["request"]

    def test_marker_file_is_0600(self) -> None:
        _post("update_system")
        marker_path = ir._marker_path("update_system")
        assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600

    def test_marker_contains_the_documented_shape(self) -> None:
        _post("remove_companion", retention="purge")
        marker = json.loads(ir._marker_path("remove_companion").read_text())
        assert marker["schema_version"] == 1
        assert marker["verb"] == "remove_companion"
        assert marker["slug"] == "safent-ads"
        assert marker["retention"] == "purge"
        assert marker["attempt"] == 1
        assert marker["created_at"].endswith("Z")
        assert marker["expires_at"].endswith("Z")

    def test_retention_defaults_to_keep(self) -> None:
        _post("remove_companion")
        marker = json.loads(ir._marker_path("remove_companion").read_text())
        assert marker["retention"] == "keep"

    def test_non_companion_verb_has_no_slug_field(self) -> None:
        _post("update_system")
        marker = json.loads(ir._marker_path("update_system").read_text())
        assert "slug" not in marker


# ---------------------------------------------------------------------------
# Idempotency — a second live request returns 409 with the existing one
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_request_while_live_is_409_with_the_existing_request(self) -> None:
        first = _post("update_system")
        assert first.status_code == 200

        second = _post("update_system")
        assert second.status_code == 409
        assert second.json()["accepted"] is False
        assert second.json()["request"]["verb"] == "update_system"

    def test_second_request_does_not_touch_the_marker_file(self) -> None:
        _post("update_system")
        marker_path = ir._marker_path("update_system")
        before = marker_path.read_text()
        time.sleep(0.01)
        _post("update_system")
        assert marker_path.read_text() == before

    def test_different_verbs_do_not_conflict(self) -> None:
        assert _post("update_system").status_code == 200
        assert _post("install_companion").status_code == 200


# ---------------------------------------------------------------------------
# Expiry — per verb, and lazily cleaned up on read
# ---------------------------------------------------------------------------


class TestExpiryPerVerb:
    @pytest.mark.parametrize(
        ("verb", "expected_ttl_s"),
        [
            ("install_companion", 30 * 60),
            ("repair_companion", 15 * 60),
            ("remove_companion", 5 * 60),
            ("update_system", 45 * 60),
            ("uninstall_system", 10 * 60),
        ],
    )
    def test_ttl_matches_the_contract_table(self, verb: str, expected_ttl_s: int) -> None:
        r = _post(verb)
        assert r.status_code == 200
        marker = json.loads(ir._marker_path(verb).read_text())
        created = ir.datetime.strptime(marker["created_at"], ir._ISO_FORMAT)
        expires = ir.datetime.strptime(marker["expires_at"], ir._ISO_FORMAT)
        assert (expires - created).total_seconds() == expected_ttl_s

    def test_expired_marker_is_treated_as_absent_and_deleted(self) -> None:
        _post("update_system")
        marker_path = ir._marker_path("update_system")
        marker = json.loads(marker_path.read_text())
        marker["expires_at"] = "2000-01-01T00:00:00Z"  # long past
        marker_path.write_text(json.dumps(marker))

        assert ir.is_verb_live("update_system") is False
        assert not marker_path.exists()

    def test_get_omits_an_expired_request_and_cleans_it_up(self) -> None:
        _post("install_companion")
        marker_path = ir._marker_path("install_companion")
        marker = json.loads(marker_path.read_text())
        marker["expires_at"] = "2000-01-01T00:00:00Z"
        marker_path.write_text(json.dumps(marker))

        body = _client().get("/api/v1/system/requests", headers=_auth_headers()).json()
        assert body["requests"] == []
        assert not marker_path.exists()

    def test_a_new_request_can_be_made_after_the_old_one_expired(self) -> None:
        _post("update_system")
        marker_path = ir._marker_path("update_system")
        marker = json.loads(marker_path.read_text())
        marker["expires_at"] = "2000-01-01T00:00:00Z"
        marker_path.write_text(json.dumps(marker))

        r = _post("update_system")
        assert r.status_code == 200  # not 409 — the expired one does not block a fresh request


# ---------------------------------------------------------------------------
# GET listing + claim state
# ---------------------------------------------------------------------------


class TestGetListing:
    def test_lists_only_verbs_with_a_live_marker(self) -> None:
        _post("update_system")
        body = _client().get("/api/v1/system/requests", headers=_auth_headers()).json()
        verbs = [r["verb"] for r in body["requests"]]
        assert verbs == ["update_system"]

    def test_fresh_claim_reports_claimed(self, _isolated_instance_dir: Path) -> None:
        _post("update_system")
        claim_path = ir._claim_path("update_system")
        claim_path.write_text('{"pid": 1}')

        body = _client().get("/api/v1/system/requests", headers=_auth_headers()).json()
        assert body["requests"][0]["state"] == "claimed"

    def test_stale_claim_reverts_to_pending(self, _isolated_instance_dir: Path) -> None:
        _post("update_system")
        claim_path = ir._claim_path("update_system")
        claim_path.write_text('{"pid": 1}')
        old = time.time() - ir._CLAIM_TTL_S - 5
        os.utime(claim_path, (old, old))

        body = _client().get("/api/v1/system/requests", headers=_auth_headers()).json()
        assert body["requests"][0]["state"] == "pending"


# ---------------------------------------------------------------------------
# Legacy aliases — behaviour preserved, dual-write for the old flat markers
# ---------------------------------------------------------------------------


class TestLegacyAliases:
    def test_post_system_update_alias_preserves_the_old_response_shape(self) -> None:
        r = _client().post("/api/v1/system/update", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json() == {"ok": True, "updating": True}

    def test_post_system_update_alias_writes_the_new_marker(self) -> None:
        _client().post("/api/v1/system/update", headers=_auth_headers())
        assert ir._marker_path("update_system").exists()

    def test_post_system_update_alias_also_writes_the_legacy_flat_flag(
        self, _isolated_instance_dir: Path
    ) -> None:
        """A `safent agent` already installed in the field only watches the
        OLD flat file — must not silently stop working (module docstring)."""
        _client().post("/api/v1/system/update", headers=_auth_headers())
        assert (_isolated_instance_dir / ".update-requested").exists()

    def test_post_system_uninstall_alias_preserves_the_old_response_shape(self) -> None:
        r = _client().post("/api/v1/system/uninstall", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_post_system_uninstall_alias_also_writes_the_legacy_flat_flag(
        self, _isolated_instance_dir: Path
    ) -> None:
        _client().post("/api/v1/system/uninstall", headers=_auth_headers())
        assert (_isolated_instance_dir / ".uninstall-requested").exists()

    def test_alias_is_idempotent_not_an_error_on_repeat(self) -> None:
        first = _client().post("/api/v1/system/update", headers=_auth_headers())
        second = _client().post("/api/v1/system/update", headers=_auth_headers())
        assert first.status_code == 200
        assert second.status_code == 200  # never a 409 at the legacy path


# ---------------------------------------------------------------------------
# is_verb_live — the shared read used by system_update.py's `updating` field
# ---------------------------------------------------------------------------


class TestIsVerbLive:
    def test_false_when_no_marker_exists(self) -> None:
        assert ir.is_verb_live("update_system") is False

    def test_true_right_after_creation(self) -> None:
        _post("update_system")
        assert ir.is_verb_live("update_system") is True

    def test_independent_per_verb(self) -> None:
        _post("update_system")
        assert ir.is_verb_live("uninstall_system") is False


# ---------------------------------------------------------------------------
# claim_request / resolve_request — T016, the host agent's own consumer
# (mutually-exclusive claim between `safent agent` and the open app,
# install-request.md §4; "fallo -> vuelve a pending, no bucle").
# ---------------------------------------------------------------------------


class TestClaimRequest:
    def test_nothing_to_claim_when_no_marker_exists(self) -> None:
        assert ir.claim_request("install_companion", claimant="agent-1") is None

    def test_claims_a_pending_request_and_returns_its_slug(self) -> None:
        _post("install_companion")
        claimed = ir.claim_request("install_companion", claimant="agent-1")
        assert claimed is not None
        assert claimed.verb == "install_companion"
        assert claimed.slug == "safent-ads"

    def test_verb_with_no_slug_claims_with_slug_none(self) -> None:
        _post("update_system")
        claimed = ir.claim_request("update_system", claimant="agent-1")
        assert claimed is not None
        assert claimed.slug is None

    def test_claim_file_is_written_0600(self) -> None:
        _post("install_companion")
        ir.claim_request("install_companion", claimant="agent-1")
        claim_path = ir._claim_path("install_companion")
        assert claim_path.read_text() == "agent-1"
        assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600

    def test_a_second_claimant_is_refused_while_the_first_claim_is_live(self) -> None:
        _post("install_companion")
        first = ir.claim_request("install_companion", claimant="agent-1")
        second = ir.claim_request("install_companion", claimant="agent-2")
        assert first is not None
        assert second is None

    def test_the_same_claimant_can_reclaim_renewing_its_own_claim(self) -> None:
        """A long-running install must be able to renew its own claim
        instead of losing it to the OTHER reader mid-flight."""
        _post("install_companion")
        first = ir.claim_request("install_companion", claimant="agent-1")
        second = ir.claim_request("install_companion", claimant="agent-1")
        assert first is not None
        assert second is not None

    def test_a_stale_claim_fails_without_automatic_reclaim(self) -> None:
        _post("install_companion")
        ir.claim_request("install_companion", claimant="agent-1")
        claim_path = ir._claim_path("install_companion")
        old = time.time() - ir._CLAIM_TTL_S - 5
        os.utime(claim_path, (old, old))

        claimed = ir.claim_request("install_companion", claimant="agent-2")
        assert claimed is None
        assert ir.list_live_requests()[0].state == "failed"

    def test_an_expired_marker_is_not_claimable_and_is_deleted(self) -> None:
        _post("install_companion")
        marker_path = ir._marker_path("install_companion")
        marker = json.loads(marker_path.read_text())
        marker["expires_at"] = "2000-01-01T00:00:00Z"
        marker_path.write_text(json.dumps(marker))

        assert ir.claim_request("install_companion", claimant="agent-1") is None
        assert not marker_path.exists()


class TestResolveRequest:
    def test_success_consumes_the_marker_and_the_claim(self) -> None:
        _post("install_companion")
        claim = ir.claim_request("install_companion", claimant="agent-1")
        assert claim is not None
        assert ir.resolve_ads_request(
            "install_companion", claimant="agent-1", request_id=claim.request_id, success=True
        )

        assert not ir._marker_path("install_companion").exists()
        assert not ir._claim_path("install_companion").exists()

    def test_failure_releases_the_claim_but_keeps_the_marker_for_a_retry(self) -> None:
        _post("install_companion")
        claim = ir.claim_request("install_companion", claimant="agent-1")
        assert claim is not None
        assert ir.resolve_ads_request(
            "install_companion", claimant="agent-1", request_id=claim.request_id, success=False
        )

        assert ir._marker_path("install_companion").exists()
        assert not ir._claim_path("install_companion").exists()

    def test_after_a_failure_explicit_new_request_is_required(self) -> None:
        _post("install_companion")
        claim = ir.claim_request("install_companion", claimant="agent-1")
        assert claim is not None
        ir.resolve_ads_request(
            "install_companion", claimant="agent-1", request_id=claim.request_id, success=False
        )

        claimed = ir.claim_request("install_companion", claimant="agent-2")
        assert claimed is None
        assert _post("install_companion").status_code == 200
        fresh = ir.claim_request("install_companion", claimant="agent-2")
        assert fresh is not None and fresh.request_id != claim.request_id
        assert not ir.resolve_ads_request(
            "install_companion", claimant="agent-1", request_id=claim.request_id, success=True
        )

    def test_resolve_without_a_prior_claim_is_a_safe_no_op(self) -> None:
        _post("install_companion")
        with pytest.raises(ValueError):
            ir.resolve_request("install_companion", success=True)
        assert ir._marker_path("install_companion").exists()
