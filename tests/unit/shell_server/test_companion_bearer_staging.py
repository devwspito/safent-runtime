"""The daemon (uid 880) must be able to READ the companion bearer (024).

`read_companion_bearer` runs inside hermes-runtime.service as uid 880. The
bearer is a HOST file bind-mounted read-only, written 0400 by provision.sh, and
its uid/gid inside the container is an ENGINE artefact — 0:0 under rootless
podman/docker, the installing owner under rootful podman. Neither is `hermes`,
so the daemon could never read it and `ADS_BEARER` stayed empty: mcp-remote sent
no credential, /mcp answered 401, and the seeded server sat on
`companion_status: esperando_servicio` forever.

Widening the host mode to 0444 is not an option — uid 886 (`hermes-sandbox`, the
agent/MCP sandbox) would get the bearer. So a full-root `ExecStartPre=-+` stages
a 0440 root:hermes copy on tmpfs. These tests pin both halves.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

from hermes.shell_server import companions as companions_mod
from hermes.shell_server.companions import (
    COMPANION_RUNTIME_BEARER_DIR,
    CompanionEndpoint,
    read_companion_bearer,
    runtime_bearer_path,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STAGING_SCRIPT_PATH = _REPO_ROOT / "ops/agents-os-edition/scripts/hermes-companion-bearer"


def _load_staging_script(module_name: str):
    """Load the extension-less staging script as a module (no `.py` — needs
    an explicit `SourceFileLoader`, mirrors test_tailscale_control.py)."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(_STAGING_SCRIPT_PATH))
    spec = importlib.util.spec_from_file_location(module_name, _STAGING_SCRIPT_PATH, loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _endpoint(bearer_ref: str) -> CompanionEndpoint:
    return CompanionEndpoint(
        slug="safent-ads",
        url="https://ads.safent.internal:8443/mcp",
        host="ads.safent.internal",
        ip="10.201.0.10",
        port=8443,
        ca_path="/etc/hermes/companions/ads-ca.crt",
        ca_fingerprint="sha256:" + "a" * 64,
        bearer_ref=bearer_ref,
    )


class TestReadCompanionBearerPrefersTheStagedCopy:
    def test_staged_copy_wins_over_the_unreadable_mount(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staged = tmp_path / "safent-ads.bearer"
        staged.write_text("staged-token\n")
        monkeypatch.setattr(companions_mod, "runtime_bearer_path", lambda _s: str(staged))
        # bearer_ref points outside the mount => branch 2 would return None
        assert read_companion_bearer(_endpoint("file:/nowhere/ads.bearer")) == "staged-token"

    def test_falls_back_to_the_mount_when_nothing_is_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mount = tmp_path / "ads.bearer"
        mount.write_text("mount-token\n")
        monkeypatch.setattr(
            companions_mod, "runtime_bearer_path", lambda _s: str(tmp_path / "absent")
        )
        monkeypatch.setattr(companions_mod, "_COMPANION_MOUNT_DIR", tmp_path)
        assert read_companion_bearer(_endpoint(f"file:{mount}")) == "mount-token"

    def test_prefer_runtime_copy_false_reads_the_source_not_last_boots_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stage-in script MUST read the mount, or a rotated bearer would be
        invisible until the tmpfs is cleared."""
        staged = tmp_path / "safent-ads.bearer"
        staged.write_text("stale-token\n")
        mount = tmp_path / "ads.bearer"
        mount.write_text("rotated-token\n")
        monkeypatch.setattr(companions_mod, "runtime_bearer_path", lambda _s: str(staged))
        monkeypatch.setattr(companions_mod, "_COMPANION_MOUNT_DIR", tmp_path)
        got = read_companion_bearer(_endpoint(f"file:{mount}"), prefer_runtime_copy=False)
        assert got == "rotated-token"

    def test_staged_path_is_derived_from_the_slug_never_from_the_json(self) -> None:
        assert runtime_bearer_path("safent-ads") == f"{COMPANION_RUNTIME_BEARER_DIR}/safent-ads.bearer"
        assert COMPANION_RUNTIME_BEARER_DIR == "/run/hermes/companions"

    def test_out_of_mount_bearer_ref_is_still_refused_when_nothing_is_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "secret"
        outside.write_text("nope\n")
        monkeypatch.setattr(
            companions_mod, "runtime_bearer_path", lambda _s: str(tmp_path / "absent")
        )
        assert read_companion_bearer(_endpoint(f"file:{outside}")) is None


class TestStagingIsWiredIntoTheImage:
    def test_runtime_unit_stages_the_bearer_as_full_root_before_the_daemon(self) -> None:
        unit = (
            _REPO_ROOT / "ops/agents-os-edition/systemd/hermes-runtime.service"
        ).read_text(encoding="utf-8")
        assert "ExecStartPre=-+/usr/libexec/hermes/hermes-companion-bearer" in unit, (
            "the stage-in must run BEFORE the daemon and with the `+` full-privilege "
            "prefix — User=hermes cannot read the bind-mounted bearer."
        )
        assert unit.index("hermes-companion-bearer") < unit.index("ExecStart=/usr/bin/hermes-runtime")

    def test_script_is_baked_into_the_image(self) -> None:
        cf = (_REPO_ROOT / "ops/container/Containerfile").read_text(encoding="utf-8")
        assert "scripts/hermes-companion-bearer /usr/libexec/hermes/hermes-companion-bearer" in cf

    def test_staged_dir_is_root_owned_group_hermes_and_not_world_readable(self) -> None:
        conf = (
            _REPO_ROOT / "ops/agents-os-edition/tmpfiles/hermes.conf"
        ).read_text(encoding="utf-8")
        line = next(
            ln for ln in conf.splitlines() if ln.startswith("d /run/hermes/companions")
        )
        fields = line.split()
        assert fields[2] == "0750", "world-readable staged bearers would reach hermes-sandbox"
        assert fields[3] == "root"
        assert fields[4] == "hermes"

    def test_script_writes_0440_root_group_hermes(self) -> None:
        script = (
            _REPO_ROOT / "ops/agents-os-edition/scripts/hermes-companion-bearer"
        ).read_text(encoding="utf-8")
        assert "0o440" in script
        assert '_DAEMON_GROUP = "hermes"' in script
        assert "prefer_runtime_copy=False" in script


class TestSsoKeyStaging:
    """ADS-02: the 026 SSO private key has the exact same unreadable-mount
    shape as the bearer, and must be staged by the SAME script/mechanism —
    see companion_sso_authority.py and test_companion_sso_assertion.py::
    TestDefaultKeyPathIsTheRootStagedCopy for the daemon-side half."""

    def test_runtime_sso_key_path_is_on_the_staged_tmpfs_dir(self) -> None:
        assert (
            companions_mod.COMPANION_RUNTIME_SSO_KEY_PATH
            == f"{COMPANION_RUNTIME_BEARER_DIR}/ads-sso.key"
        )

    def test_mount_path_is_the_fixed_companion_secrets_directory(self) -> None:
        assert (
            companions_mod.COMPANION_SSO_KEY_MOUNT_PATH
            == "/etc/hermes/companions/ads-sso.key"
        )

    def test_script_stages_the_sso_key_from_the_mount_to_the_runtime_copy(self) -> None:
        script = (
            _REPO_ROOT / "ops/agents-os-edition/scripts/hermes-companion-bearer"
        ).read_text(encoding="utf-8")
        assert "COMPANION_SSO_KEY_MOUNT_PATH" in script
        assert "COMPANION_RUNTIME_SSO_KEY_PATH" in script
        assert "_stage_sso_key" in script
        assert "_stage_sso_key(gid)" in script, "must actually run from main(), not just be defined"

    def test_script_end_to_end_reads_the_mount_and_calls_write_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runs the real `_stage_sso_key` against a fake mount, with
        `_write_secret` swapped for a spy: `_write_secret`'s real body calls
        `os.fchown(fd, 0, gid)` (root-owned copy, see `_write_secret`
        itself), which requires CAP_CHOWN a plain test process doesn't have
        — exactly the same reason none of the existing bearer tests call it
        for real either. What matters here is `_stage_sso_key` reads the
        MOUNT (not a stale copy) and hands the trimmed value + the runtime
        path to the SAME `_write_secret` the bearer uses (0440 root:hermes,
        pinned by test_script_writes_0440_root_group_hermes)."""
        module = _load_staging_script("hermes_companion_bearer_sso_stage")

        mount = tmp_path / "ads-sso.key"
        mount.write_text("fake-seed-b64\n")
        runtime_copy = tmp_path / "run" / "ads-sso.key"
        monkeypatch.setattr(module, "COMPANION_SSO_KEY_MOUNT_PATH", str(mount))
        monkeypatch.setattr(module, "COMPANION_RUNTIME_SSO_KEY_PATH", str(runtime_copy))
        calls: list[tuple[str, str, int]] = []
        monkeypatch.setattr(
            module, "_write_secret", lambda path, value, gid: calls.append((path, value, gid))
        )

        staged = module._stage_sso_key(gid=42)  # noqa: SLF001

        assert staged == 1
        assert calls == [(str(runtime_copy), "fake-seed-b64", 42)]

    def test_absent_mount_purges_a_stale_runtime_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_staging_script("hermes_companion_bearer_sso_purge")

        stale = tmp_path / "run" / "ads-sso.key"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale-seed\n")
        monkeypatch.setattr(module, "COMPANION_SSO_KEY_MOUNT_PATH", str(tmp_path / "absent"))
        monkeypatch.setattr(module, "COMPANION_RUNTIME_SSO_KEY_PATH", str(stale))

        staged = module._stage_sso_key(gid=os.getgid())  # noqa: SLF001

        assert staged == 0
        assert not stale.exists()
