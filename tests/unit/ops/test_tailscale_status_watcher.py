"""Tests for the hermes-tailscale-status-watcher sidecar (022).

Contract: specs/022-tailnet-connectivity/contracts.md §2 (binding — the
egress lane's readers, MagicDnsSuffixSource and shell_server.tailnet.api,
are already implemented against this exact schema):

    {"node_name": str, "magicdns_suffix": str, "tailnet": str, "online": bool,
     "peers": [{"name": str, "online": bool}, ...]}

Split like the source: pure `status_document()` transform tests (no I/O), a
handful of `refresh_once()` tests with a mocked subprocess boundary, and a
fake `tailscale` binary on PATH proving the real subprocess call + atomic
write + file mode.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPT_PATH = (
    Path(__file__).parents[3]
    / "ops"
    / "agents-os-edition"
    / "scripts"
    / "hermes-tailscale-status-watcher"
)


def _load_script():
    loader = importlib.machinery.SourceFileLoader(
        "hermes_tailscale_status_watcher", str(_SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_tailscale_status_watcher", _SCRIPT_PATH, loader=loader
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["hermes_tailscale_status_watcher"] = m
    spec.loader.exec_module(m)
    return m


mod = _load_script()


# ---------------------------------------------------------------------------
# Pure status_document() — no I/O
# ---------------------------------------------------------------------------


class TestStatusDocumentOnline:
    def test_running_node_reports_online_true(self) -> None:
        doc = mod.status_document({"BackendState": "Running"})
        assert doc["online"] is True

    def test_needs_login_reports_online_false(self) -> None:
        doc = mod.status_document({"BackendState": "NeedsLogin"})
        assert doc["online"] is False

    def test_node_name_prefers_host_name(self) -> None:
        doc = mod.status_document(
            {"BackendState": "Running", "Self": {"HostName": "safent-agent", "DNSName": "x.ts.net."}}
        )
        assert doc["node_name"] == "safent-agent"

    def test_node_name_falls_back_to_dns_name_first_label(self) -> None:
        doc = mod.status_document(
            {"BackendState": "Running", "Self": {"DNSName": "safent-agent.tail1234.ts.net."}}
        )
        assert doc["node_name"] == "safent-agent"

    def test_node_name_empty_when_neither_present(self) -> None:
        doc = mod.status_document({"BackendState": "Running", "Self": {}})
        assert doc["node_name"] == ""

    def test_magicdns_suffix_and_tailnet_name(self) -> None:
        doc = mod.status_document(
            {
                "BackendState": "Running",
                "CurrentTailnet": {"MagicDNSSuffix": "tail1234.ts.net", "Name": "acme.ts.net"},
            }
        )
        assert doc["magicdns_suffix"] == "tail1234.ts.net"
        assert doc["tailnet"] == "acme.ts.net"

    def test_missing_optional_sections_default_to_empty_strings_not_missing_keys(self) -> None:
        doc = mod.status_document({"BackendState": "Running"})
        assert doc["magicdns_suffix"] == ""
        assert doc["tailnet"] == ""
        assert doc["node_name"] == ""
        assert set(doc.keys()) == {"node_name", "magicdns_suffix", "tailnet", "online", "peers"}


class TestStatusDocumentPeers:
    def test_peers_names_and_online_only(self) -> None:
        doc = mod.status_document(
            {
                "BackendState": "Running",
                "Peer": {
                    "nodekey:aaa": {"HostName": "laptop", "Online": True},
                    "nodekey:bbb": {"HostName": "server", "Online": False},
                },
            }
        )
        assert doc["peers"] == [
            {"name": "laptop", "online": True},
            {"name": "server", "online": False},
        ]

    def test_peers_sorted_by_name_for_determinism(self) -> None:
        doc = mod.status_document(
            {
                "BackendState": "Running",
                "Peer": {
                    "1": {"HostName": "zeta", "Online": True},
                    "2": {"HostName": "alpha", "Online": True},
                },
            }
        )
        assert [p["name"] for p in doc["peers"]] == ["alpha", "zeta"]

    def test_peer_without_a_derivable_name_is_dropped(self) -> None:
        doc = mod.status_document({"BackendState": "Running", "Peer": {"x": {"Online": True}}})
        assert doc["peers"] == []

    def test_peers_never_carry_ip_tags_or_fingerprints(self) -> None:
        doc = mod.status_document(
            {
                "BackendState": "Running",
                "Peer": {
                    "1": {
                        "HostName": "laptop",
                        "Online": True,
                        "TailscaleIPs": ["100.64.0.5"],
                        "KeyExpiry": "2027-01-01T00:00:00Z",
                        "Tags": ["tag:x"],
                        "OS": "linux",
                    }
                },
            }
        )
        assert doc["peers"] == [{"name": "laptop", "online": True}]

    def test_peers_empty_when_this_node_is_not_online(self) -> None:
        """A cached netmap from a node that is itself not Running is not
        trustworthy — report none rather than guess (documented assumption)."""
        doc = mod.status_document(
            {
                "BackendState": "NeedsLogin",
                "Peer": {"1": {"HostName": "laptop", "Online": True}},
            }
        )
        assert doc["peers"] == []

    def test_non_dict_peer_entries_are_skipped_not_crashed_on(self) -> None:
        doc = mod.status_document({"BackendState": "Running", "Peer": {"1": "not-a-dict"}})
        assert doc["peers"] == []

    def test_malformed_top_level_sections_do_not_crash(self) -> None:
        doc = mod.status_document({"BackendState": "Running", "Self": "oops", "CurrentTailnet": []})
        assert doc["node_name"] == ""
        assert doc["magicdns_suffix"] == ""
        assert doc["tailnet"] == ""


# ---------------------------------------------------------------------------
# refresh_once() — I/O boundary, mocked subprocess
# ---------------------------------------------------------------------------


@pytest.fixture()
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime_dir = tmp_path / "run-tailscale"
    runtime_dir.mkdir(mode=0o711)
    status_file = runtime_dir / "status.json"
    socket_path = runtime_dir / "tailscaled.sock"
    last_attempt_file = runtime_dir / "last-attempt.json"
    monkeypatch.setattr(mod, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(mod, "STATUS_FILE", status_file)
    monkeypatch.setattr(mod, "TS_SOCKET", socket_path)
    monkeypatch.setattr(mod, "LAST_ATTEMPT_FILE", last_attempt_file)
    return {"runtime_dir": runtime_dir, "status_file": status_file, "last_attempt_file": last_attempt_file}


class TestRefreshOnce:
    def test_writes_status_json_on_success(self, paths: dict) -> None:
        fake_result = MagicMock(
            returncode=0, stdout=json.dumps({"BackendState": "Running"})
        )
        with patch.object(mod, "_run_status", return_value=fake_result):
            wrote = mod.refresh_once()
        assert wrote is True
        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert payload["online"] is True

    def test_writes_even_when_cli_exits_nonzero_but_json_is_valid(self, paths: dict) -> None:
        """`tailscale status --json` famously exits non-zero for NeedsLogin
        while still emitting a complete document — gating on returncode would
        silently skip writing exactly the state callers most need to see."""
        fake_result = MagicMock(returncode=1, stdout=json.dumps({"BackendState": "NeedsLogin"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            wrote = mod.refresh_once()
        assert wrote is True
        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert payload["online"] is False

    def test_leaves_status_json_untouched_when_subprocess_unavailable(self, paths: dict) -> None:
        paths["status_file"].write_text('{"online": true, "stale": "marker"}', encoding="utf-8")
        with patch.object(mod, "_run_status", return_value=None):
            wrote = mod.refresh_once()
        assert wrote is False
        assert "stale" in paths["status_file"].read_text(encoding="utf-8")

    def test_leaves_status_json_untouched_on_malformed_json(self, paths: dict) -> None:
        paths["status_file"].write_text('{"online": true, "stale": "marker"}', encoding="utf-8")
        fake_result = MagicMock(returncode=0, stdout="not json {{{")
        with patch.object(mod, "_run_status", return_value=fake_result):
            wrote = mod.refresh_once()
        assert wrote is False
        assert "stale" in paths["status_file"].read_text(encoding="utf-8")

    def test_status_file_is_0644(self, paths: dict) -> None:
        fake_result = MagicMock(returncode=0, stdout=json.dumps({"BackendState": "Running"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            mod.refresh_once()
        mode = stat.S_IMODE(paths["status_file"].stat().st_mode)
        assert mode == 0o644

    def test_never_writes_a_key_even_if_present_in_tailscale_output(self, paths: dict) -> None:
        fake_result = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "BackendState": "Running",
                    "AuthKey": "tskey-should-never-appear",
                    "Self": {"HostName": "n", "PrivateKey": "should-never-appear-either"},
                }
            ),
        )
        with patch.object(mod, "_run_status", return_value=fake_result):
            mod.refresh_once()
        raw = paths["status_file"].read_text(encoding="utf-8")
        assert "should-never-appear" not in raw
        assert "tskey" not in raw
        assert "AuthKey" not in raw
        assert "PrivateKey" not in raw


# ---------------------------------------------------------------------------
# last_attempt mirroring (025 hallazgo D): hermes-tailscale-control writes
# its connect verdict to last-attempt.json (no key material); this watcher
# folds it into status.json's own last_attempt field so `configured`/`online`
# can mean "logged in" without losing the "a connect attempt just failed"
# signal — see tailnet/api.py's redefinition of `configured`.
# ---------------------------------------------------------------------------


class TestLastAttemptMirroring:
    def test_failed_attempt_is_mirrored_into_status_json(self, paths: dict) -> None:
        paths["last_attempt_file"].write_text(
            json.dumps({"at": "2026-09-10T14:03:00+00:00", "ok": False, "error_kind": "tailscale_up_failed"}),
            encoding="utf-8",
        )
        fake_result = MagicMock(returncode=1, stdout=json.dumps({"BackendState": "NeedsLogin"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            mod.refresh_once()

        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert payload["online"] is False
        assert payload["last_attempt"] == {
            "at": "2026-09-10T14:03:00+00:00", "ok": False, "error_kind": "tailscale_up_failed",
        }

    def test_successful_attempt_is_mirrored_too(self, paths: dict) -> None:
        paths["last_attempt_file"].write_text(
            json.dumps({"at": "2026-09-10T14:05:00+00:00", "ok": True, "error_kind": None}),
            encoding="utf-8",
        )
        fake_result = MagicMock(returncode=0, stdout=json.dumps({"BackendState": "Running"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            mod.refresh_once()

        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert payload["last_attempt"]["ok"] is True

    def test_no_last_attempt_file_omits_the_field_not_a_guess(self, paths: dict) -> None:
        fake_result = MagicMock(returncode=0, stdout=json.dumps({"BackendState": "Running"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            mod.refresh_once()

        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert "last_attempt" not in payload

    def test_corrupt_last_attempt_file_is_ignored_fail_soft(self, paths: dict) -> None:
        paths["last_attempt_file"].write_text("not json {{{", encoding="utf-8")
        fake_result = MagicMock(returncode=0, stdout=json.dumps({"BackendState": "Running"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            wrote = mod.refresh_once()

        assert wrote is True
        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert "last_attempt" not in payload

    def test_last_attempt_never_carries_a_key_even_if_someone_sneaks_one_in(self, paths: dict) -> None:
        """Defense in depth: even if last-attempt.json somehow contained a
        stray key-shaped field, only the three known fields are mirrored."""
        paths["last_attempt_file"].write_text(
            json.dumps({
                "at": "2026-09-10T14:03:00+00:00", "ok": False, "error_kind": "tailscale_up_failed",
                "auth_key": "tskey-should-never-appear",
            }),
            encoding="utf-8",
        )
        fake_result = MagicMock(returncode=0, stdout=json.dumps({"BackendState": "Running"}))
        with patch.object(mod, "_run_status", return_value=fake_result):
            mod.refresh_once()

        raw = paths["status_file"].read_text(encoding="utf-8")
        assert "tskey" not in raw
        assert "auth_key" not in raw


# ---------------------------------------------------------------------------
# Fake `tailscale` binary on PATH — real subprocess boundary
# ---------------------------------------------------------------------------

_FAKE_TAILSCALE = """#!/usr/bin/env bash
if [ "$1" = "--socket" ]; then shift 2; fi
if [ "$1" = "status" ]; then
  cat "$FAKE_TAILSCALE_STATUS_JSON"
  exit "${FAKE_TAILSCALE_RC:-0}"
fi
exit 1
"""


@pytest.fixture()
def fake_tailscale_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    ts = bin_dir / "tailscale"
    ts.write_text(_FAKE_TAILSCALE)
    ts.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    status_json = tmp_path / "fake-status.json"
    status_json.write_text(
        json.dumps(
            {
                "BackendState": "Running",
                "Self": {"HostName": "safent-agent"},
                "CurrentTailnet": {"MagicDNSSuffix": "tail1234.ts.net", "Name": "acme.ts.net"},
                "Peer": {"1": {"HostName": "laptop", "Online": True}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_TAILSCALE_STATUS_JSON", str(status_json))
    return status_json


class TestFakeBinaryOnPath:
    def test_refresh_once_via_real_subprocess(self, paths: dict, fake_tailscale_on_path: Path) -> None:
        wrote = mod.refresh_once()
        assert wrote is True
        payload = json.loads(paths["status_file"].read_text(encoding="utf-8"))
        assert payload == {
            "node_name": "safent-agent",
            "magicdns_suffix": "tail1234.ts.net",
            "tailnet": "acme.ts.net",
            "online": True,
            "peers": [{"name": "laptop", "online": True}],
        }
