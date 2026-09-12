from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hermes.shell_server import install_request_agent_cli as cli
from hermes.shell_server import install_requests as ir

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "_INSTANCE_DIR", tmp_path / "instance")
    monkeypatch.setitem(
        sys.modules,
        "hermes.agents_os.infrastructure.companion_sso_authority",
        SimpleNamespace(_load_sso_private_key=object),
    )


def test_concurrent_claim_has_one_winner_and_shares_install_repair_lock():
    ir.create_request("install_companion", slug="safent-ads")
    ir.create_request("repair_companion", slug="safent-ads")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda n: ir.claim_request(
                    "install_companion" if n % 2 else "repair_companion", claimant=f"native-{n}"
                ),
                range(8),
            )
        )
    assert sum(claim is not None for claim in claims) == 1


def test_renew_long_work_and_reject_stale_ack():
    verb = "install_companion"
    ir.create_request(verb, slug="safent-ads")
    claim = ir.claim_request(verb, claimant="native")
    assert claim
    for _ in range(12):
        old = time.time() - 40
        os.utime(ir._claim_path(verb), (old, old))
        assert ir.renew_ads_request(verb, claimant="native", request_id=claim.request_id)
        assert ir.claim_request(verb, claimant="other") is None
    assert not ir.resolve_ads_request(
        verb, claimant="other", request_id=claim.request_id, success=True
    )
    assert not ir.resolve_ads_request(verb, claimant="native", request_id="f" * 32, success=True)
    old = time.time() - 61
    os.utime(ir._claim_path(verb), (old, old))
    assert not ir.resolve_ads_request(
        verb, claimant="native", request_id=claim.request_id, success=True
    )
    assert not ir.renew_ads_request(verb, claimant="native", request_id=claim.request_id)
    assert ir.list_live_requests()[0].state == "failed"
    assert ir.claim_request(verb, claimant="restarted") is None


@pytest.mark.parametrize(
    "injected",
    [
        {"image": "evil:latest"},
        {"url": "https://evil"},
        {"command": "id"},
        {"schema_version": True},
        {"request_id": []},
    ],
)
def test_marker_cannot_supply_authority(injected):
    verb = "install_companion"
    ir.create_request(verb, slug="safent-ads")
    marker = ir._read_marker(verb)
    marker.update(injected)
    ir._write_marker_atomic(verb, marker)
    assert ir.claim_request(verb, claimant="native") is None
    assert ir.list_live_requests()[0].last_failure["code"] == "invalid_request"


def test_tick_does_not_claim_legacy_and_rejects_removal(capsys):
    ir.create_request("update_system")
    ir.create_request("uninstall_system")
    ir.create_request("remove_companion", slug="safent-ads")
    assert cli.main(["claim-ads", "--claimant", "native"]) == 1
    assert capsys.readouterr().out == ""
    states = {s.verb: s.state for s in ir.list_live_requests()}
    assert states == {
        "update_system": "pending",
        "uninstall_system": "pending",
        "remove_companion": "failed",
    }
    assert not ir._claim_path("update_system").exists()


def test_native_tick_never_reclaims_even_if_process_id_is_reused(capsys):
    ir.create_request("install_companion", slug="safent-ads")
    assert cli.main(["claim-ads", "--claimant", "native-123"]) == 0
    capsys.readouterr()
    assert cli.main(["claim-ads", "--claimant", "native-123"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "state,reachable,ok,expected",
    [
        ("ready", True, True, 0),
        ("no_accounts", True, True, 0),
        ("not_installed", True, True, 1),
        ("unauthorized", True, True, 1),
        ("ready", False, True, 1),
        ("ready", True, "true", 1),
    ],
)
def test_real_daemon_health_contract(monkeypatch, state, reachable, ok, expected):
    async def reload(slug):
        assert slug == "safent-ads"
        return {"ok": ok, "state": state, "reachable": reachable}

    monkeypatch.setitem(
        sys.modules,
        "hermes.shell_server.companion_reload_cli",
        SimpleNamespace(_reload_companion_presence=reload),
    )
    assert cli.main(["verify-ads"]) == expected


def test_health_errors_never_echo_secrets(monkeypatch, capsys):
    async def reload(slug):
        assert slug == "safent-ads"
        raise RuntimeError("secret-bearer")

    monkeypatch.setitem(
        sys.modules,
        "hermes.shell_server.companion_reload_cli",
        SimpleNamespace(_reload_companion_presence=reload),
    )
    assert cli.main(["verify-ads"]) == 1
    assert "secret-bearer" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["verify-ads", "health-ads"])
def test_missing_or_invalid_sso_never_reports_ready(monkeypatch, capsys, command):
    def unavailable():
        raise ValueError("private-key-secret-missing")

    monkeypatch.setitem(
        sys.modules,
        "hermes.agents_os.infrastructure.companion_sso_authority",
        SimpleNamespace(_load_sso_private_key=unavailable),
    )
    assert cli.main([command]) == 1
    assert "private-key-secret" not in capsys.readouterr().err
