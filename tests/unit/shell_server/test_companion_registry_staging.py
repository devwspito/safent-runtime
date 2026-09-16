"""Root source staging, daemon trust and stale-authority removal."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes.shell_server import companions as c
from tests.unit.shell_server.test_companion_bearer_staging import (
    _endpoint,
    _load_staging_script,
)


def test_default_registry_is_staged_not_host_source():
    assert Path("/run/hermes/companions/companions.json") == c._COMPANIONS_PATH
    assert Path("/etc/hermes/companions/companions.json") == c.COMPANION_SOURCE_REGISTRY_PATH


@pytest.mark.parametrize(
    "uid,mode,parent_uid,parent_mode,accepted",
    [
        (0, 0o100440, 0, 0o40750, True),
        (880, 0o100440, 0, 0o40750, False),
        (1000, 0o100440, 0, 0o40750, False),
        (0, 0o100660, 0, 0o40750, False),
        (0, 0o120440, 0, 0o40750, False),
        (0, 0o100440, 880, 0o40750, False),
        (0, 0o100440, 0, 0o40770, False),
        (0, 0o100440, 0, 0o120750, False),
    ],
)
def test_staged_registry_requires_root_regular_protected_directory(
    monkeypatch, uid, mode, parent_uid, parent_mode, accepted
):
    path = c._COMPANIONS_PATH
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _p: SimpleNamespace(
            st_uid=uid if _p == path else parent_uid,
            st_mode=mode if _p == path else parent_mode,
        ),
    )
    assert c._is_root_staged_registry(path) is accepted
    if not accepted:
        monkeypatch.setattr(c, "_is_trustworthy_file", lambda _p: True)
        monkeypatch.setattr(
            Path, "read_text", lambda *_a, **_kw: pytest.fail("read untrusted registry")
        )
        assert c.load_companions() == {}


def test_virtiofs_caller_owner_is_not_accepted_as_daemon_source(monkeypatch, tmp_path):
    path = tmp_path / "source.json"
    caller = [0]
    monkeypatch.setattr(c.os, "geteuid", lambda: caller[0])
    monkeypatch.setattr(c.os, "statvfs", lambda _p: SimpleNamespace(f_flag=c.os.ST_RDONLY))
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *_a, **_kw: SimpleNamespace(
            st_uid=caller[0],
            st_mode=0o100400,
        ),
    )
    assert c._is_trustworthy_file(path)
    caller[0] = 880
    assert not c._is_trustworthy_file(path)


@pytest.fixture
def staging(tmp_path, monkeypatch):
    module = _load_staging_script("registry_staging_fixture")
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setattr(module, "COMPANION_RUNTIME_BEARER_DIR", str(root))
    monkeypatch.setattr(module, "COMPANION_RUNTIME_REGISTRY_PATH", root / "companions.json")
    monkeypatch.setattr(module, "COMPANION_RUNTIME_SSO_KEY_PATH", str(root / "ads-sso.key"))
    monkeypatch.setattr(module, "runtime_bearer_path", lambda slug: str(root / f"{slug}.bearer"))
    monkeypatch.setattr(module, "_ensure_dir", lambda _gid: None)
    monkeypatch.setattr(module.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=880))
    monkeypatch.setattr(module.os, "fchown", lambda _fd, _uid, _gid: None)
    for name in ["companions.json", "safent-ads.bearer", "ads-sso.key"]:
        (root / name).write_text("STALE")
    return module, root


def test_invalid_source_purges_all_stale_authority(staging, monkeypatch):
    module, root = staging
    calls = []
    monkeypatch.setattr(module, "load_companions", lambda **kw: calls.append(kw) or {})
    monkeypatch.setattr(
        module, "_stage_sso_key", lambda _gid: pytest.fail("invalid source stages SSO")
    )
    assert module.main() == 0
    assert calls == [{"path": c.COMPANION_SOURCE_REGISTRY_PATH}]
    assert list(root.iterdir()) == []


def test_registry_published_after_credentials_and_only_validated_fields(staging, monkeypatch):
    module, root = staging
    endpoint = _endpoint("file:/etc/hermes/companions/ads.bearer")
    monkeypatch.setattr(module, "load_companions", lambda **_kw: {"safent-ads": endpoint})
    monkeypatch.setattr(module, "read_companion_bearer", lambda _e, **_kw: "FAKE-token")

    def sso(_gid):
        assert not (root / "companions.json").exists()
        assert (root / "safent-ads.bearer").read_text() == "FAKE-token"
        return 0

    monkeypatch.setattr(module, "_stage_sso_key", sso)
    assert module.main() == 0
    data = json.loads((root / "companions.json").read_text())
    assert data["companions"][0]["ca_path"] == endpoint.ca_path
    assert data["companions"][0]["slug"] == "safent-ads"
    assert "FAKE-token" not in json.dumps(data)
    assert (root / "companions.json").stat().st_mode & 0o777 == 0o440
    assert not list(root.glob(".companion-*"))


def test_missing_bearer_cannot_reuse_old_copy_or_publish_registry(staging, monkeypatch):
    module, root = staging
    monkeypatch.setattr(
        module, "load_companions", lambda **_kw: {"safent-ads": _endpoint("file:/missing")}
    )
    monkeypatch.setattr(module, "read_companion_bearer", lambda _e, **_kw: None)
    assert module.main() == 0
    assert list(root.iterdir()) == []


def test_partial_staging_failure_purges_credentials(staging, monkeypatch):
    module, root = staging
    monkeypatch.setattr(
        module, "load_companions", lambda **_kw: {"safent-ads": _endpoint("file:/source")}
    )
    monkeypatch.setattr(module, "read_companion_bearer", lambda _e, **_kw: "FAKE-token")

    def fail(_gid):
        raise OSError("fixture failure")

    monkeypatch.setattr(module, "_stage_sso_key", fail)
    with pytest.raises(OSError):
        module.main()
    assert list(root.iterdir()) == []


def test_nft_root_uses_explicit_source_before_daemon_staging():
    root = Path(__file__).resolve().parents[3]
    source = (root / "ops/agents-os-edition/scripts/hermes-companion-nft").read_text()
    assert "load_companions(path=COMPANION_SOURCE_REGISTRY_PATH)" in source
