"""JsonHostAllowlistStore — persistent per-host SSH grant (survives restart:
a fresh store instance over the SAME path must see prior grants)."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from hermes.tailnet_ssh.infrastructure.json_host_allowlist_store import JsonHostAllowlistStore

pytestmark = pytest.mark.unit


class TestJsonHostAllowlistStore:
    def test_unknown_host_not_allowed_by_default(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        assert store.is_allowed("db1.tailxxxx.ts.net") is False

    def test_allow_then_is_allowed(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        assert store.is_allowed("db1.tailxxxx.ts.net") is True

    def test_allow_is_case_insensitive(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("DB1.tailxxxx.ts.net")
        assert store.is_allowed("db1.tailxxxx.ts.net") is True

    def test_allow_is_idempotent(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        store.allow("db1.tailxxxx.ts.net")
        assert store.list_allowed() == frozenset({"db1.tailxxxx.ts.net"})

    def test_persists_across_new_store_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        JsonHostAllowlistStore(path).allow("db1.tailxxxx.ts.net")

        reopened = JsonHostAllowlistStore(path)
        assert reopened.is_allowed("db1.tailxxxx.ts.net") is True

    def test_other_hosts_remain_unaffected(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        assert store.is_allowed("build-box.tailxxxx.ts.net") is False

    def test_revoke_removes_a_host(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        store.revoke("db1.tailxxxx.ts.net")
        assert store.is_allowed("db1.tailxxxx.ts.net") is False

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "allowlist.json"
        store = JsonHostAllowlistStore(path)
        store.allow("db1.tailxxxx.ts.net")
        assert path.exists()

    def test_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "missing.json")
        assert store.list_allowed() == frozenset()

    def test_corrupt_file_is_allowed_fails_closed(self, tmp_path: Path) -> None:
        """B1 (security review): is_allowed is the ONE reader that catches
        AllowlistStoreUnavailableError — "not yet allowed" is itself the
        safe answer (forces re-approval, never grants)."""
        path = tmp_path / "allowlist.json"
        path.write_text("not json", encoding="utf-8")
        store = JsonHostAllowlistStore(path)
        assert store.is_allowed("db1.tailxxxx.ts.net") is False

    def test_corrupt_file_list_allowed_raises_not_silently_empty(
        self, tmp_path: Path
    ) -> None:
        """B1: a corrupt file must be DISTINGUISHABLE from a genuinely empty
        store — silently returning frozenset() would be indistinguishable
        from "no hosts ever granted", hiding real data loss/corruption."""
        from hermes.tailnet_ssh.domain.errors import AllowlistStoreUnavailableError

        path = tmp_path / "allowlist.json"
        path.write_text("not json", encoding="utf-8")
        store = JsonHostAllowlistStore(path)
        with pytest.raises(AllowlistStoreUnavailableError):
            store.list_allowed()


class TestListWithMetadata:
    def test_allow_records_an_approved_at_timestamp(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")

        entries = store.list_with_metadata()

        assert len(entries) == 1
        assert entries[0].host == "db1.tailxxxx.ts.net"
        assert entries[0].approved_at  # non-empty ISO-8601 string

    def test_entries_sorted_by_host(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("zzz.tailxxxx.ts.net")
        store.allow("aaa.tailxxxx.ts.net")

        hosts = [e.host for e in store.list_with_metadata()]

        assert hosts == ["aaa.tailxxxx.ts.net", "zzz.tailxxxx.ts.net"]

    def test_revoke_removes_metadata_too(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        store.revoke("db1.tailxxxx.ts.net")

        assert store.list_with_metadata() == []

    def test_persisted_shape_is_a_dict_of_hosts_to_approved_at(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        JsonHostAllowlistStore(path).allow("db1.tailxxxx.ts.net")

        raw = json.loads(path.read_text(encoding="utf-8"))

        assert isinstance(raw["hosts"], dict)
        assert "approved_at" in raw["hosts"]["db1.tailxxxx.ts.net"]

    def test_reads_older_bare_array_shape_for_backward_compat(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        path.write_text(json.dumps({"hosts": ["db1.tailxxxx.ts.net"]}), encoding="utf-8")

        store = JsonHostAllowlistStore(path)

        assert store.is_allowed("db1.tailxxxx.ts.net") is True
        entries = store.list_with_metadata()
        assert len(entries) == 1
        assert entries[0].host == "db1.tailxxxx.ts.net"
        assert entries[0].approved_at is None
        assert entries[0].managed_by is None


class TestManagedBy:
    """spec 002 US3 (D-4): `managed_by` distinguishes an owner-granted local
    host from one Enterprise's governed-SSH policy granted via config-sync."""

    def test_local_grant_has_no_managed_by(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")

        assert store.list_with_metadata()[0].managed_by is None

    def test_cloud_grant_records_managed_by(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))

        assert store.list_with_metadata()[0].managed_by == "cloud"

    def test_allow_governed_shadows_a_pre_existing_local_grant(
        self, tmp_path: Path
    ) -> None:
        """Security review 2026-09, B2 — FR-014 ("lo heredado manda"): a
        cloud grant SHADOWS a pre-existing local entry for the same host —
        the cloud ceiling becomes authoritative, the local approval is
        preserved as an inert `superseded_local_approved_at` marker rather
        than destroyed."""
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        first_approved_at = store.list_with_metadata()[0].approved_at

        shadowed = store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",)
        )

        assert shadowed is True
        entry = store.list_with_metadata()[0]
        assert entry.managed_by == "cloud"
        assert entry.identity == "deploy"
        assert entry.capabilities == ("exec",)
        assert entry.superseded_local_approved_at == first_approved_at

    def test_allow_governed_reports_no_shadow_for_a_fresh_host(
        self, tmp_path: Path
    ) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        shadowed = store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",)
        )
        assert shadowed is False

    def test_allow_governed_reports_no_shadow_when_updating_an_existing_cloud_grant(
        self, tmp_path: Path
    ) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))
        shadowed = store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("file_read",)
        )
        assert shadowed is False

    def test_allow_governed_is_idempotent_for_an_unchanged_cloud_grant(
        self, tmp_path: Path
    ) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))

        entries = store.list_with_metadata()
        assert len(entries) == 1
        assert entries[0].managed_by == "cloud"
        assert entries[0].capabilities == ("exec",)

    def test_allow_governed_updates_the_ceiling_of_an_existing_cloud_grant(
        self, tmp_path: Path
    ) -> None:
        """Re-applying a bundle that narrows capabilities or changes
        identity updates the stored ceiling — the cloud grant is never
        stuck at its FIRST-ever shape."""
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec", "file_read")
        )

        store.allow_governed("db1.tailxxxx.ts.net", identity="auditor", capabilities=("file_read",))

        entry = store.list_with_metadata()[0]
        assert entry.identity == "auditor"
        assert entry.capabilities == ("file_read",)

    def test_allow_governed_preserves_approved_at_across_an_update(
        self, tmp_path: Path
    ) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))
        first_approved_at = store.list_with_metadata()[0].approved_at

        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("file_read",))

        assert store.list_with_metadata()[0].approved_at == first_approved_at

    def test_capabilities_are_canonicalised_sorted_deduplicated(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed(
            "db1.tailxxxx.ts.net",
            identity="deploy",
            capabilities=("file_write", "exec", "exec"),
        )

        assert store.list_with_metadata()[0].capabilities == ("exec", "file_write")

    def test_unknown_capability_is_dropped_not_granted(self, tmp_path: Path) -> None:
        """Fail-closed: a corrupted/forward-incompatible capability string
        never widens what a cloud-managed host may do."""
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec", "sudo")
        )

        assert store.list_with_metadata()[0].capabilities == ("exec",)

    def test_persisted_shape_carries_managed_by_identity_capabilities(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "allowlist.json"
        JsonHostAllowlistStore(path).allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",)
        )

        raw = json.loads(path.read_text(encoding="utf-8"))
        entry = raw["hosts"]["db1.tailxxxx.ts.net"]

        assert entry["managed_by"] == "cloud"
        assert entry["identity"] == "deploy"
        assert entry["capabilities"] == ["exec"]

    def test_older_persisted_shape_without_managed_by_defaults_to_none(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "allowlist.json"
        path.write_text(
            json.dumps({"hosts": {"db1.tailxxxx.ts.net": {"approved_at": "2026-01-01T00:00:00+00:00"}}}),
            encoding="utf-8",
        )

        store = JsonHostAllowlistStore(path)

        entry = store.list_with_metadata()[0]
        assert entry.managed_by is None
        assert entry.identity is None
        assert entry.capabilities is None


class TestGrantFor:
    """HostGrantPort implementation — the capability ceiling read back for
    the tailnet_ssh use cases (spec 002 US3, D-4)."""

    def test_absent_host_returns_none(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        assert store.grant_for("db1.tailxxxx.ts.net") is None

    def test_local_grant_has_no_ceiling(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")

        grant = store.grant_for("db1.tailxxxx.ts.net")

        assert grant is not None
        assert grant.managed_by is None
        assert grant.identity is None
        assert grant.capabilities is None

    def test_cloud_grant_has_identity_and_capabilities(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec", "file_read")
        )

        grant = store.grant_for("db1.tailxxxx.ts.net")

        assert grant is not None
        assert grant.managed_by == "cloud"
        assert grant.identity == "deploy"
        assert grant.capabilities == frozenset({"exec", "file_read"})

    def test_grant_for_is_case_insensitive(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))

        assert store.grant_for("DB1.tailxxxx.ts.net") is not None


class TestB1CeilingFailsClosedOnCorruption:
    """Security review 2026-09, B1 (CWE-636) — the capability ceiling must
    NEVER fail open when the store cannot be read. Before this fix,
    `_load()` swallowed ANY read/parse error to `{}`, so `grant_for` on a
    corrupted file returned None indistinguishably from "genuinely no
    grant" — capability_ceiling.resolve_execution_identity then treated
    that as "no ceiling" and let a cloud-managed host execute unrestricted
    with the default remote user."""

    def test_grant_for_raises_on_corrupt_file_never_returns_none(
        self, tmp_path: Path
    ) -> None:
        from hermes.tailnet_ssh.domain.errors import AllowlistStoreUnavailableError

        path = tmp_path / "allowlist.json"
        store = JsonHostAllowlistStore(path)
        store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("file_read",)
        )

        # Simulate a crash mid os.replace / a torn read: the file on disk is
        # truncated JSON.
        path.write_text(
            '{"hosts": {"db1.tailxxxx.ts.net": {"approved_at": "x", "managed_by": "clo',
            encoding="utf-8",
        )

        with pytest.raises(AllowlistStoreUnavailableError):
            store.grant_for("db1.tailxxxx.ts.net")

    def test_is_allowed_still_fails_closed_on_the_same_corruption(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "allowlist.json"
        store = JsonHostAllowlistStore(path)
        store.allow_governed(
            "db1.tailxxxx.ts.net", identity="deploy", capabilities=("file_read",)
        )
        path.write_text("not json", encoding="utf-8")

        assert store.is_allowed("db1.tailxxxx.ts.net") is False


class TestB1WriteNeverClobbersOnCorruption:
    """Security review 2026-09, B1 — a read-modify-write method must NEVER
    treat "file unreadable" as "file empty" and rewrite it: that silently
    discards every surviving entry (e.g. a crash mid-write followed by a
    local `allow()` used to wipe out every cloud-managed grant)."""

    def test_allow_refuses_to_write_over_a_corrupt_file(self, tmp_path: Path) -> None:
        from hermes.tailnet_ssh.domain.errors import AllowlistStoreUnavailableError

        path = tmp_path / "allowlist.json"
        corrupt_bytes = "not json at all"
        path.write_text(corrupt_bytes, encoding="utf-8")
        store = JsonHostAllowlistStore(path)

        with pytest.raises(AllowlistStoreUnavailableError):
            store.allow("build-box.tailxxxx.ts.net")

        # The file is untouched — no data was silently discarded.
        assert path.read_text(encoding="utf-8") == corrupt_bytes

    def test_allow_governed_refuses_to_write_over_a_corrupt_file(
        self, tmp_path: Path
    ) -> None:
        from hermes.tailnet_ssh.domain.errors import AllowlistStoreUnavailableError

        path = tmp_path / "allowlist.json"
        corrupt_bytes = "not json at all"
        path.write_text(corrupt_bytes, encoding="utf-8")
        store = JsonHostAllowlistStore(path)

        with pytest.raises(AllowlistStoreUnavailableError):
            store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))

        assert path.read_text(encoding="utf-8") == corrupt_bytes

    def test_revoke_refuses_to_write_over_a_corrupt_file(self, tmp_path: Path) -> None:
        from hermes.tailnet_ssh.domain.errors import AllowlistStoreUnavailableError

        path = tmp_path / "allowlist.json"
        corrupt_bytes = "not json at all"
        path.write_text(corrupt_bytes, encoding="utf-8")
        store = JsonHostAllowlistStore(path)

        with pytest.raises(AllowlistStoreUnavailableError):
            store.revoke("db1.tailxxxx.ts.net")

        assert path.read_text(encoding="utf-8") == corrupt_bytes


class TestB1AtomicWrite:
    def test_save_leaves_no_leftover_tmp_files(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")

        leftovers = [p for p in tmp_path.iterdir() if ".tmp-" in p.name]
        assert leftovers == []

    def test_failed_write_leaves_original_file_untouched_and_no_tmp_leftover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "allowlist.json"
        store = JsonHostAllowlistStore(path)
        store.allow("db1.tailxxxx.ts.net")
        original_bytes = path.read_bytes()

        def _boom(_fd: int) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "fsync", _boom)

        with pytest.raises(OSError, match="disk full"):
            store.allow("build-box.tailxxxx.ts.net")

        assert path.read_bytes() == original_bytes
        leftovers = [p for p in tmp_path.iterdir() if ".tmp-" in p.name]
        assert leftovers == []

    def test_file_mode_is_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        JsonHostAllowlistStore(path).allow("db1.tailxxxx.ts.net")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_parent_dir_mode_is_0700(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested" / "allowlist.json"
        JsonHostAllowlistStore(nested).allow("db1.tailxxxx.ts.net")
        assert stat.S_IMODE(nested.parent.stat().st_mode) == 0o700

    def test_lock_file_is_created_alongside(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        JsonHostAllowlistStore(path).allow("db1.tailxxxx.ts.net")
        assert (tmp_path / "allowlist.json.lock").exists()


class TestB1ConcurrentReadModifyWriteSerialized:
    """Security review 2026-09, B1 — two interleaved read-modify-write
    cycles must never lose an update (the reported repro: two racing
    cycles resurrected a revoked cloud grant). The flock around the WHOLE
    read+write critical section serializes them."""

    def test_concurrent_allow_governed_calls_never_lose_a_host(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "allowlist.json"
        hosts = [f"host{i}.tailxxxx.ts.net" for i in range(40)]
        barrier = threading.Barrier(2)

        def _grant_half(subset: list[str]) -> None:
            store = JsonHostAllowlistStore(path)
            barrier.wait()
            for host in subset:
                store.allow_governed(host, identity="deploy", capabilities=("exec",))

        t1 = threading.Thread(target=_grant_half, args=(hosts[:20],))
        t2 = threading.Thread(target=_grant_half, args=(hosts[20:],))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        final = JsonHostAllowlistStore(path).list_allowed()
        assert final == frozenset(hosts)

    def test_revoke_racing_allow_governed_never_resurrects_after_revoke_wins_last(
        self, tmp_path: Path
    ) -> None:
        """A tighter race on the SAME host: N alternating
        allow_governed/revoke cycles from two threads must never crash and
        must always leave the store in a state consistent with SOME
        serialized ordering (never a torn read, never a lost write)."""
        path = tmp_path / "allowlist.json"
        store = JsonHostAllowlistStore(path)
        host = "db1.tailxxxx.ts.net"
        iterations = 60
        errors: list[BaseException] = []

        def _allow_loop() -> None:
            try:
                for _ in range(iterations):
                    store.allow_governed(host, identity="deploy", capabilities=("exec",))
            except BaseException as exc:  # noqa: BLE001 — surfaced via `errors`
                errors.append(exc)

        def _revoke_loop() -> None:
            try:
                for _ in range(iterations):
                    store.revoke(host)
            except BaseException as exc:  # noqa: BLE001 — surfaced via `errors`
                errors.append(exc)

        t1 = threading.Thread(target=_allow_loop)
        t2 = threading.Thread(target=_revoke_loop)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert errors == []
        # No assertion on the FINAL allowed/revoked state (racing on purpose,
        # either final state is a valid serialization) — the guarantee under
        # test is that every read the store returns is well-formed JSON,
        # never a torn/interleaved write.
        JsonHostAllowlistStore(path).list_allowed()  # must not raise


class TestB2CloudRevokeRestoresShadowedLocal:
    """Security review 2026-09, B2 — the local approval a cloud grant
    shadowed was never destroyed; revoking the cloud grant restores it."""

    def test_revoke_restores_the_shadowed_local_entry(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        local_approved_at = store.list_with_metadata()[0].approved_at
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))

        restored = store.revoke("db1.tailxxxx.ts.net")

        assert restored is True
        entry = store.list_with_metadata()[0]
        assert entry.managed_by is None
        assert entry.identity is None
        assert entry.approved_at == local_approved_at

    def test_revoke_of_a_fresh_cloud_grant_does_not_restore_anything(
        self, tmp_path: Path
    ) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))

        restored = store.revoke("db1.tailxxxx.ts.net")

        assert restored is False
        assert store.list_with_metadata() == []

    def test_revoke_of_a_plain_local_entry_reports_no_restoration(
        self, tmp_path: Path
    ) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")

        restored = store.revoke("db1.tailxxxx.ts.net")

        assert restored is False


class TestB2GovernedFlag:
    """Security review 2026-09, B2c — an instance becomes permanently
    `governed` the first time it receives a real cloud grant."""

    def test_never_governed_by_default(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        assert store.is_governed() is False

    def test_local_allow_alone_never_governs(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow("db1.tailxxxx.ts.net")
        assert store.is_governed() is False

    def test_allow_governed_marks_the_instance_governed(self, tmp_path: Path) -> None:
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))
        assert store.is_governed() is True

    def test_governed_stays_true_after_the_only_cloud_grant_is_revoked(
        self, tmp_path: Path
    ) -> None:
        """Once governed, always governed — an org that stops publishing an
        `ssh` section does not silently reopen local HITL approval for
        brand-new hosts (default-deny stays the safer posture)."""
        store = JsonHostAllowlistStore(tmp_path / "allowlist.json")
        store.allow_governed("db1.tailxxxx.ts.net", identity="deploy", capabilities=("exec",))
        store.revoke("db1.tailxxxx.ts.net")

        assert store.is_governed() is True

    def test_is_governed_fails_closed_to_true_on_corruption(self, tmp_path: Path) -> None:
        """Uncertain -> assume governed -> keep local approval CLOSED. The
        unsafe direction here is "openly approve a brand new local host",
        so corruption must not default to "not governed"."""
        path = tmp_path / "allowlist.json"
        path.write_text("not json", encoding="utf-8")
        store = JsonHostAllowlistStore(path)

        assert store.is_governed() is True
