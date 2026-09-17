"""DbusRuntimeServiceWiring's governed-SSH verbs (spec 002 US3, D-4).

Exercises list_ssh_hosts / allow_ssh_host / revoke_ssh_host directly on the
wiring (no D-Bus bus required) — the counterpart of
tests/unit/config_sync/test_applier_access_scope_integration.py, which
proves the SAME verbs are reachable through the real
Runtime1ServiceInterface.

Path override technique: JsonHostAllowlistStore/StatusJsonTailnetDirectory
default their path via a class-level default argument, bound once at import
time — monkeypatching the module-level constant has no effect on an
already-defined `__init__`. Rebinding `__init__.__defaults__` for the
duration of a test is the narrow, restorable way to point the wiring's
internal (locally-imported, no constructor injection — mirrors
add_egress_domain's own hardcoded-default-path style) instantiation at a
tmp_path file instead of /var/lib/hermes or /run/hermes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tailnet_ssh.infrastructure.json_host_allowlist_store import (
    JsonHostAllowlistStore,
)
from hermes.tailnet_ssh.infrastructure.status_json_directory import (
    StatusJsonTailnetDirectory,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999

_STATUS_WITH_DB1 = {
    "node_name": "self",
    "magicdns_suffix": "tailxxxx.ts.net",
    "online": True,
    "peers": [{"name": "db1", "online": True}, {"name": "build-box", "online": True}],
}


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring() -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
    )


@pytest.fixture
def ssh_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Repoint JsonHostAllowlistStore/StatusJsonTailnetDirectory's default
    construction (no-arg `Class()`, as the wiring calls them) at tmp_path."""
    allow_path = tmp_path / "ssh-allowlist.json"
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(JsonHostAllowlistStore.__init__, "__defaults__", (allow_path,))
    monkeypatch.setattr(
        StatusJsonTailnetDirectory.__init__, "__defaults__", (status_path,)
    )
    return allow_path, status_path


def _write_status(status_path: Path, status: dict = _STATUS_WITH_DB1) -> None:
    status_path.write_text(json.dumps(status), encoding="utf-8")


def _draft(host: str, *, identity: str = "agent", capabilities: list[str] | None = None) -> str:
    return json.dumps(
        {"host": host, "identity": identity, "capabilities": capabilities or ["exec"]}
    )


@pytest.mark.usefixtures("ssh_paths")
class TestListSshHosts:
    def test_empty_allowlist_returns_empty_list(self) -> None:
        wiring = _make_wiring()
        assert wiring.list_ssh_hosts() == []

    def test_requires_no_authorization(self) -> None:
        """Read-only verb — mirrors list_egress_grants (no authZ check)."""
        wiring = _make_wiring()
        # No _authorize_and_resolve call is made; an unauthorized-looking
        # context still succeeds because the method never checks sender_uid.
        assert wiring.list_ssh_hosts() == []

    def test_lists_hosts_with_managed_by(self, ssh_paths: tuple[Path, Path]) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()
        wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_OPERATOR_UID)

        entries = wiring.list_ssh_hosts()

        assert entries == [
            {
                "host": "db1.tailxxxx.ts.net",
                "approved_at": entries[0]["approved_at"],
                "managed_by": "cloud",
            }
        ]


@pytest.mark.usefixtures("ssh_paths")
class TestAllowSshHostAuthorization:
    def test_unauthorized_sender_raises(self) -> None:
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_UNAUTHORIZED_UID)

    def test_unauthorized_sender_never_persists(self, ssh_paths: tuple[Path, Path]) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_UNAUTHORIZED_UID)
        assert wiring.list_ssh_hosts() == []


class TestAllowSshHostResolution:
    """REQ-20 — host membership is resolved ONLY against the live tailnet
    directory (never DNS, never /etc/hosts)."""

    def test_bare_peer_name_resolves_to_canonical_fqdn(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_OPERATOR_UID)

        assert resp == {"ok": True, "host": "db1.tailxxxx.ts.net"}

    def test_fqdn_under_suffix_resolves(self, ssh_paths: tuple[Path, Path]) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(
            draft_json=_draft("db1.tailxxxx.ts.net"), sender_uid=_OPERATOR_UID
        )

        assert resp == {"ok": True, "host": "db1.tailxxxx.ts.net"}

    def test_non_tailnet_host_is_permanently_rejected_never_applied(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        """T071a — a host that resolves OUTSIDE the tailnet is rejected, and
        the allow-list is left untouched (never `applied`)."""
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(
            draft_json=_draft("evil.example.com"), sender_uid=_OPERATOR_UID
        )

        assert resp == {"ok": False, "error": "unknown_host"}
        assert wiring.list_ssh_hosts() == []

    def test_unlisted_name_under_the_suffix_is_rejected(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        """I1/REQ-20 (security review 2026-09) — the governed path never
        trusts a name merely for being under the tailnet's own suffix; it
        must ALSO be a currently-listed peer."""
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(
            draft_json=_draft("ghost.tailxxxx.ts.net"), sender_uid=_OPERATOR_UID
        )

        assert resp == {"ok": False, "error": "unknown_host"}
        assert wiring.list_ssh_hosts() == []

    def test_multi_label_name_under_the_suffix_is_rejected(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(
            draft_json=_draft("a.b.tailxxxx.ts.net"), sender_uid=_OPERATOR_UID
        )

        assert resp == {"ok": False, "error": "unknown_host"}

    def test_ip_literal_host_is_rejected_as_invalid(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(draft_json=_draft("100.64.1.2"), sender_uid=_OPERATOR_UID)

        assert resp == {"ok": False, "error": "invalid_host"}

    @pytest.mark.usefixtures("ssh_paths")
    def test_missing_status_file_is_a_directory_unavailable_error(self) -> None:
        """No status.json (tailscaled never wrote one) — transitory, distinct
        from a permanent host rejection so the applier can retry."""
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_OPERATOR_UID)

        assert resp == {"ok": False, "error": "directory_unavailable"}

    def test_malformed_draft_json_is_rejected(self) -> None:
        """Draft parsing fails before any file I/O — no path override needed."""
        wiring = _make_wiring()
        resp = wiring.allow_ssh_host(draft_json="not json", sender_uid=_OPERATOR_UID)
        assert resp == {"ok": False, "error": "invalid_draft"}

    def test_draft_missing_host_key_is_rejected(self) -> None:
        wiring = _make_wiring()
        resp = wiring.allow_ssh_host(
            draft_json=json.dumps({"identity": "agent", "capabilities": ["exec"]}),
            sender_uid=_OPERATOR_UID,
        )
        assert resp == {"ok": False, "error": "invalid_draft"}


class TestAllowSshHostShadowsLocal:
    """Security review 2026-09, B2 — FR-014 ("lo heredado manda"): a cloud
    grant SHADOWS a pre-existing LOCAL (non-cloud) entry — the cloud
    ceiling applies immediately; the local approval is preserved as an
    inert marker, never destroyed."""

    def test_pre_existing_local_host_is_shadowed_by_the_cloud_grant(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        JsonHostAllowlistStore(allow_path).allow("build-box.tailxxxx.ts.net")
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(draft_json=_draft("build-box"), sender_uid=_OPERATOR_UID)

        assert resp == {"ok": True, "host": "build-box.tailxxxx.ts.net", "shadowed_local": True}
        entries = wiring.list_ssh_hosts()
        assert entries == [
            {
                "host": "build-box.tailxxxx.ts.net",
                "approved_at": entries[0]["approved_at"],
                "managed_by": "cloud",
            }
        ]

    def test_shadowing_preserves_the_local_approval_as_an_inert_marker(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        JsonHostAllowlistStore(allow_path).allow("build-box.tailxxxx.ts.net")
        local_approved_at = (
            JsonHostAllowlistStore(allow_path).list_with_metadata()[0].approved_at
        )
        wiring = _make_wiring()

        wiring.allow_ssh_host(draft_json=_draft("build-box"), sender_uid=_OPERATOR_UID)

        entry = JsonHostAllowlistStore(allow_path).list_with_metadata()[0]
        assert entry.superseded_local_approved_at == local_approved_at

    def test_shadowing_enforces_the_cloud_ceiling(self, ssh_paths: tuple[Path, Path]) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        JsonHostAllowlistStore(allow_path).allow("build-box.tailxxxx.ts.net")
        wiring = _make_wiring()

        wiring.allow_ssh_host(
            draft_json=_draft("build-box", capabilities=["file_read"]),
            sender_uid=_OPERATOR_UID,
        )

        grant = JsonHostAllowlistStore(allow_path).grant_for("build-box.tailxxxx.ts.net")
        assert grant is not None
        assert grant.managed_by == "cloud"
        assert grant.capabilities == frozenset({"file_read"})


class TestAllowSshHostIdempotent:
    def test_re_applying_the_same_cloud_grant_is_a_no_op_success(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()
        wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_OPERATOR_UID)

        resp = wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_OPERATOR_UID)

        assert resp == {"ok": True, "host": "db1.tailxxxx.ts.net"}
        assert len(wiring.list_ssh_hosts()) == 1


class TestAllowSshHostNarrowing:
    """Re-applying a bundle that narrows capabilities or changes identity
    updates the stored ceiling (spec 002 US3, D-4)."""

    def test_re_applying_with_narrower_capabilities_updates_the_ceiling(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()
        wiring.allow_ssh_host(
            draft_json=_draft("db1", capabilities=["exec", "file_read"]),
            sender_uid=_OPERATOR_UID,
        )

        wiring.allow_ssh_host(
            draft_json=_draft("db1", capabilities=["file_read"]), sender_uid=_OPERATOR_UID
        )

        grant = JsonHostAllowlistStore(allow_path).grant_for("db1.tailxxxx.ts.net")
        assert grant is not None
        assert grant.capabilities == frozenset({"file_read"})

    def test_re_applying_with_a_different_identity_updates_the_ceiling(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()
        wiring.allow_ssh_host(
            draft_json=_draft("db1", identity="deploy"), sender_uid=_OPERATOR_UID
        )

        wiring.allow_ssh_host(
            draft_json=_draft("db1", identity="auditor"), sender_uid=_OPERATOR_UID
        )

        grant = JsonHostAllowlistStore(allow_path).grant_for("db1.tailxxxx.ts.net")
        assert grant is not None
        assert grant.identity == "auditor"


class TestAllowSshHostInvalidGrantShape:
    def test_missing_identity_is_rejected(self, ssh_paths: tuple[Path, Path]) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(
            draft_json=json.dumps({"host": "db1", "capabilities": ["exec"]}),
            sender_uid=_OPERATOR_UID,
        )

        assert resp == {"ok": False, "error": "invalid_draft"}

    def test_empty_capabilities_is_rejected(self, ssh_paths: tuple[Path, Path]) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()

        resp = wiring.allow_ssh_host(
            draft_json=json.dumps({"host": "db1", "identity": "deploy", "capabilities": []}),
            sender_uid=_OPERATOR_UID,
        )

        assert resp == {"ok": False, "error": "invalid_draft"}


@pytest.mark.usefixtures("ssh_paths")
class TestRevokeSshHost:
    def test_unauthorized_sender_raises(self) -> None:
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.revoke_ssh_host(host="db1.tailxxxx.ts.net", sender_uid=_UNAUTHORIZED_UID)

    def test_revokes_a_cloud_managed_host(self, ssh_paths: tuple[Path, Path]) -> None:
        _, status_path = ssh_paths
        _write_status(status_path)
        wiring = _make_wiring()
        wiring.allow_ssh_host(draft_json=_draft("db1"), sender_uid=_OPERATOR_UID)

        resp = wiring.revoke_ssh_host(host="db1.tailxxxx.ts.net", sender_uid=_OPERATOR_UID)

        assert resp == {"ok": True, "revoked": True, "restored_local": False}
        assert wiring.list_ssh_hosts() == []

    def test_revoking_a_cloud_grant_that_shadowed_a_local_one_restores_it(
        self, ssh_paths: tuple[Path, Path]
    ) -> None:
        """Security review 2026-09, B2 — the local approval was dormant,
        not deleted; revoking the cloud grant restores it."""
        allow_path, status_path = ssh_paths
        _write_status(status_path)
        JsonHostAllowlistStore(allow_path).allow("build-box.tailxxxx.ts.net")
        wiring = _make_wiring()
        wiring.allow_ssh_host(draft_json=_draft("build-box"), sender_uid=_OPERATOR_UID)

        resp = wiring.revoke_ssh_host(
            host="build-box.tailxxxx.ts.net", sender_uid=_OPERATOR_UID
        )

        assert resp == {"ok": True, "revoked": True, "restored_local": True}
        entries = wiring.list_ssh_hosts()
        assert entries == [
            {
                "host": "build-box.tailxxxx.ts.net",
                "approved_at": entries[0]["approved_at"],
                "managed_by": None,
            }
        ]

    def test_never_revokes_a_local_grant(self, ssh_paths: tuple[Path, Path]) -> None:
        """Removing a grant from the published bundle must revoke only hosts
        managed_by Enterprise — a human's own local grant is untouched."""
        allow_path, _ = ssh_paths
        JsonHostAllowlistStore(allow_path).allow("build-box.tailxxxx.ts.net")
        wiring = _make_wiring()

        resp = wiring.revoke_ssh_host(
            host="build-box.tailxxxx.ts.net", sender_uid=_OPERATOR_UID
        )

        assert resp == {"ok": True, "revoked": False}
        assert wiring.list_ssh_hosts() == [
            {
                "host": "build-box.tailxxxx.ts.net",
                "approved_at": wiring.list_ssh_hosts()[0]["approved_at"],
                "managed_by": None,
            }
        ]

    def test_revoking_an_absent_host_is_a_no_op(self) -> None:
        wiring = _make_wiring()
        resp = wiring.revoke_ssh_host(host="nowhere.tailxxxx.ts.net", sender_uid=_OPERATOR_UID)
        assert resp == {"ok": True, "revoked": False}
