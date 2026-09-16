"""JsonHostAllowlistStore — persistent per-host SSH grant (survives restart:
a fresh store instance over the SAME path must see prior grants)."""

from __future__ import annotations

import json
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

    def test_corrupt_file_reads_as_empty_fail_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        path.write_text("not json", encoding="utf-8")
        store = JsonHostAllowlistStore(path)
        assert store.list_allowed() == frozenset()
        assert store.is_allowed("db1.tailxxxx.ts.net") is False


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
