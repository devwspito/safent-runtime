"""ops/container/companions/ads/provision.sh — end-to-end host-side
provisioning (024). Runs the REAL script + the REAL compose.yaml/
caps.template.yaml (`HERE` resolves to the actual repo directory), with only
the two commands that would touch a real container engine or the network
faked: `podman` (network/image/gen_keys/compose) and `curl` (/mcp/health).
Everything else (openssl, mv, chmod...) is the real host tool,
exactly like a real run.

Covers: secrets are generated once and never re-generated, ADS_MCP_TOKEN
mirrors the bearer, broker.env carries the gen_keys public half, caps.yaml
is fail-closed (accounts: {}, autonomy_enabled: false), no secret value
ever reaches stdout/stderr, and a second run is a true no-op except for
merging an owner-provided vendor.env exactly once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROVISION_SH = _REPO_ROOT / "ops/container/companions/ads/provision.sh"
_COMPOSE_YAML = _REPO_ROOT / "ops/container/companions/ads/compose.yaml"
_CAPS_TEMPLATE = _REPO_ROOT / "ops/container/companions/ads/caps.template.yaml"

_FAKE_PODMAN = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"
case "$1 $2" in
  "network inspect")
    echo "10.201.0.0/24"
    exit 0
    ;;
  "network create")
    exit 0
    ;;
  "image inspect")
    exit 0
    ;;
esac
if [ "$1" = "run" ]; then
  for a in "$@"; do
    if [ "$a" = "safent_ads.tools.gen_keys" ]; then
      echo "ADS_APPROVAL_SIGNING_KEY=ZmFrZS1zaWduaW5nLWtleS1iNjQ="
      echo "ADS_APPROVAL_PUBLIC_KEY=ZmFrZS1wdWJsaWMta2V5LWI2NA=="
      exit 0
    fi
  done
  exit 0
fi
if [ "$1" = "compose" ] || [ "$1" = "pull" ]; then
  exit 0
fi
exit 0
"""

_FAKE_CURL = """#!/usr/bin/env bash
# /mcp/health is bearer-protected: a bare 401 IS liveness (see provision.sh).
[ -z "${FAKE_CURL_LOG:-}" ] || printf '%s\\n' "$@" >> "$FAKE_CURL_LOG"
printf '401'
exit 0
"""

# gen_keys prints STANDARD base64 (the fake above) for BOTH the approval
# keypair (unchanged, T193/024) and the 026 SSO keypair (T001) —
# provision.sh calls gen_keys twice, once per pair. Kept byte-identical to
# the value test_gitleaks_allowlist.py pins (it must stay literally present
# in this file, or that allowlist entry becomes dead weight) — it happens
# to contain no `+`/`/`, so TestSsoKeypairUrlSafeConversion below uses ITS
# OWN fake with different values to actually exercise the alphabet swap.
_GEN_KEYS_PUBLIC = "ZmFrZS1wdWJsaWMta2V5LWI2NA=="
_GEN_KEYS_PUBLIC_URLSAFE = _GEN_KEYS_PUBLIC


@pytest.fixture()
def fake_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(_FAKE_PODMAN)
    podman.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o755)
    return bin_dir


def _run_provision(
    state_dir: Path, fake_bin_dir: Path, podman_log: Path, *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"
    env["SAFENT_COMPANION_STATE"] = str(state_dir)
    env["SAFENT_ADS_IMAGE"] = "safent-ads:test-fake"
    env["FAKE_PODMAN_LOG"] = str(podman_log)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(_PROVISION_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _hash_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


@pytest.fixture()
def provisioned_state(
    tmp_path: Path, fake_bin_dir: Path
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    state_dir = tmp_path / "state"
    podman_log = tmp_path / "podman.log"
    result = _run_provision(state_dir, fake_bin_dir, podman_log)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return state_dir, result


@pytest.mark.parametrize("platform,ip", [("Darwin", "127.0.0.1"), ("Linux", "10.201.0.10")])
def test_host_health_probe_uses_exact_route_and_private_tls(tmp_path, fake_bin_dir, platform, ip):
    uname = fake_bin_dir / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{platform}'\n")
    uname.chmod(0o755)
    log = tmp_path / "curl.log"
    result = _run_provision(tmp_path / "state", fake_bin_dir, tmp_path / "podman.log",
                            extra_env={"FAKE_CURL_LOG": str(log)})
    assert result.returncode == 0, result.stderr
    arguments = log.read_text().splitlines()
    assert f"ads.safent.internal:8443:{ip}" in arguments
    assert "--cacert" in arguments
    assert "--noproxy" in arguments
    assert "--insecure" not in arguments


def test_scaffold_does_not_depend_on_gnu_sha256sum(tmp_path: Path, fake_bin_dir: Path) -> None:
    """The desktop app starts with a macOS-style minimal PATH.  A poisoned
    sha256sum proves the scaffold derives the CA fingerprint through the
    already-required OpenSSL implementation instead."""
    sha256sum = fake_bin_dir / "sha256sum"
    sha256sum.write_text("#!/bin/sh\necho sha256sum-must-not-run >&2\nexit 93\n")
    sha256sum.chmod(0o755)

    result = subprocess.run(
        ["bash", str(_PROVISION_SH), "--scaffold"],
        env={
            **os.environ,
            "PATH": f"{fake_bin_dir}:/usr/bin:/bin",
            "SAFENT_COMPANION_STATE": str(tmp_path / "state"),
            "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
            "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "sha256sum-must-not-run" not in result.stderr
    assert (tmp_path / "state" / "companions.json").is_file()


class TestFirstRunWritesExpectedFiles:
    def test_bearer_is_0400(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        assert _mode(state_dir / "bearer") == 0o400

    def test_leaf_key_is_readable_by_the_container_uid(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        # ads-api (uid 10001) monta tls/ de solo lectura: a 0600 del host no
        # podia leer la clave y entraba en bucle de arranque (T214). El
        # directorio de estado (0700) es lo que la protege de otros usuarios.
        state_dir, _ = provisioned_state
        assert _mode(state_dir / "tls" / "leaf.key") == 0o644
        assert _mode(state_dir) == 0o700

    def test_api_env_is_0600_and_mcp_token_equals_the_bearer(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        api_env = state_dir / "secrets" / "api.env"
        assert _mode(api_env) == 0o600
        bearer = (state_dir / "bearer").read_text().strip()
        lines = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in api_env.read_text().splitlines() if "=" in ln and not ln.startswith("#")}
        assert lines["ADS_MCP_TOKEN"] == bearer
        assert lines["ADS_APPROVAL_SIGNING_KEY"] == "ZmFrZS1zaWduaW5nLWtleS1iNjQ="
        for required in ("ADS_SESSION_SECRET", "ADS_TOTP_ENC_KEY"):
            assert lines[required], f"{required} missing or empty"

    def test_broker_env_is_0600_and_has_the_public_key_from_gen_keys(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        broker_env = state_dir / "secrets" / "broker.env"
        assert _mode(broker_env) == 0o600
        text = broker_env.read_text()
        assert f"ADS_APPROVAL_PUBLIC_KEY={_GEN_KEYS_PUBLIC}" in text
        assert "ADS_BROKER_ALLOWED_UIDS=10001" in text
        assert "ADS_BROKER_HARD_CAPS_FILE=/etc/ads-broker/caps.yaml" in text

    def test_caps_yaml_is_fail_closed(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        caps_path = state_dir / "caps.yaml"
        assert _mode(caps_path) == 0o644
        doc = yaml.safe_load(caps_path.read_text())
        assert doc["accounts"] == {}
        assert doc["defaults"]["autonomy_enabled"] is False

    def test_companions_json_is_0444(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        assert _mode(state_dir / "companions.json") == 0o444

    def test_image_marker_records_the_image_this_run_actually_used(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        """CLI-10: `safent companion status/rotate/remove` read this file
        instead of falling back to a hard-coded ghcr.io/…/safent-ads:latest
        that could silently diverge from what provisioning actually used
        (run-safent.sh's own dev convenience picks safent-ads:local when it
        exists locally)."""
        state_dir, _ = provisioned_state
        assert (state_dir / "image").read_text() == "safent-ads:test-fake"

    def test_no_secret_value_reaches_stdout_or_stderr(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, result = provisioned_state
        api_env = (state_dir / "secrets" / "api.env").read_text()
        broker_env = (state_dir / "secrets" / "broker.env").read_text()
        secrets: list[str] = []
        for text in (api_env, broker_env):
            for line in text.splitlines():
                if "=" in line and not line.startswith("#"):
                    _, _, value = line.partition("=")
                    if value:
                        secrets.append(value)
        combined_output = result.stdout + result.stderr
        for secret in secrets:
            assert secret not in combined_output, f"secret value leaked into output: {secret!r}"


class TestSsoKeypairProvisioning:
    """026, contracts/sso.md §3 — the SSO Ed25519 pair provisioned alongside
    the bearer: private half 0400 on the host, public half handed to the
    companion via secrets/api.env, never argv/log."""

    def test_private_key_is_0400(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        assert _mode(state_dir / "sso" / "ads-sso.key") == 0o400

    def test_sso_dir_is_0700(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        assert _mode(state_dir / "sso") == 0o700

    def test_public_key_in_api_env_is_url_safe_base64(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, _ = provisioned_state
        api_env = (state_dir / "secrets" / "api.env").read_text()
        lines = {
            ln.split("=", 1)[0]: ln.split("=", 1)[1]
            for ln in api_env.splitlines()
            if "=" in ln and not ln.startswith("#")
        }
        assert lines["ADS_SSO_PUBLIC_KEY"] == _GEN_KEYS_PUBLIC_URLSAFE
        assert "+" not in lines["ADS_SSO_PUBLIC_KEY"]
        assert "/" not in lines["ADS_SSO_PUBLIC_KEY"]

    def test_private_key_never_reaches_stdout_or_stderr(
        self, provisioned_state: tuple[Path, subprocess.CompletedProcess[str]]
    ) -> None:
        state_dir, result = provisioned_state
        seed = (state_dir / "sso" / "ads-sso.key").read_text().strip()
        combined_output = result.stdout + result.stderr
        assert seed not in combined_output


# gen_keys public halves can legitimately contain base64's `+`/`/` chars;
# the shared fake above (_GEN_KEYS_PUBLIC) happens not to — it is pinned
# byte-identical to test_gitleaks_allowlist.py's allowlisted value. This
# fake is used ONLY by TestSsoKeypairUrlSafeConversion below, so it can use
# a value that actually exercises the standard->url-safe base64 swap
# without touching the gitleaks-pinned literal.
_FAKE_PODMAN_SPECIAL_CHARS_PUBLIC = _FAKE_PODMAN.replace(
    "ADS_APPROVAL_PUBLIC_KEY=ZmFrZS1wdWJsaWMta2V5LWI2NA==",
    "ADS_APPROVAL_PUBLIC_KEY=AAAA+BBBB/CCCC==",
)


class TestSsoKeypairUrlSafeConversion:
    """`tr '+/' '-_'` must actually swap the alphabet, not just pass a value
    through that never contained those characters (the everyday fake used
    elsewhere in this file for the gitleaks-allowlist reason above)."""

    def test_plus_and_slash_are_swapped_to_dash_and_underscore(
        self, tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        podman = bin_dir / "podman"
        podman.write_text(_FAKE_PODMAN_SPECIAL_CHARS_PUBLIC)
        podman.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(_FAKE_CURL)
        curl.chmod(0o755)

        state_dir = tmp_path / "state"
        result = _run_provision(state_dir, bin_dir, tmp_path / "podman.log")

        assert result.returncode == 0, result.stderr
        api_env = (state_dir / "secrets" / "api.env").read_text()
        lines = {
            ln.split("=", 1)[0]: ln.split("=", 1)[1]
            for ln in api_env.splitlines()
            if "=" in ln and not ln.startswith("#")
        }
        assert lines["ADS_SSO_PUBLIC_KEY"] == "AAAA-BBBB_CCCC=="


class TestSecondRunIsIdempotent:
    def test_second_run_changes_no_file(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_dir = tmp_path / "state"
        podman_log = tmp_path / "podman.log"
        first = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert first.returncode == 0, first.stderr
        before = _hash_tree(state_dir)

        second = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert second.returncode == 0, second.stderr
        after = _hash_tree(state_dir)

        assert before == after

    def test_second_run_does_not_regenerate_the_sso_keypair(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_dir = tmp_path / "state"
        podman_log = tmp_path / "podman.log"
        first = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert first.returncode == 0, first.stderr
        seed_before = (state_dir / "sso" / "ads-sso.key").read_bytes()
        pub_before = (state_dir / "secrets" / "api.env").read_text()

        second = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert second.returncode == 0, second.stderr
        assert (state_dir / "sso" / "ads-sso.key").read_bytes() == seed_before
        assert (state_dir / "secrets" / "api.env").read_text() == pub_before

    def test_vendor_env_is_merged_once_and_only_once(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_dir = tmp_path / "state"
        podman_log = tmp_path / "podman.log"
        first = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert first.returncode == 0, first.stderr

        vendor_env = state_dir / "vendor.env"
        vendor_env.write_text(
            "# owner-provided vendor credentials\n"
            "GOOGLE_ADS_CLIENT_ID=vendor-client-id\n"
            "GOOGLE_ADS_UNKNOWN=must-not-be-copied\n"
            "META_APP_ID=vendor-meta-app-id\n"
        )
        vendor_env.chmod(0o600)

        second = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert second.returncode == 0, second.stderr
        broker_env_after_merge = (state_dir / "secrets" / "broker.env").read_text()
        assert "GOOGLE_ADS_CLIENT_ID=vendor-client-id" in broker_env_after_merge
        assert "GOOGLE_ADS_UNKNOWN" not in broker_env_after_merge
        assert "META_APP_ID=vendor-meta-app-id" in broker_env_after_merge
        hash_after_merge = hashlib.sha256(
            (state_dir / "secrets" / "broker.env").read_bytes()
        ).hexdigest()

        third = _run_provision(state_dir, fake_bin_dir, podman_log)
        assert third.returncode == 0, third.stderr
        hash_after_third_run = hashlib.sha256(
            (state_dir / "secrets" / "broker.env").read_bytes()
        ).hexdigest()

        assert hash_after_merge == hash_after_third_run
        assert broker_env_after_merge.count("GOOGLE_ADS_CLIENT_ID=") == 1
        assert broker_env_after_merge.count("META_APP_ID=") == 1


class TestScaffoldMode:
    """028 T015 — `provision.sh --scaffold`: network + TLS + bearer +
    companions.json only, local and image-independent. Safent's own
    container always gets a valid companions.json bind-mount source from
    its FIRST boot, even before `safent companion install` ever runs — so
    installing the companion later never recreates Safent."""

    def test_scaffold_never_pulls_or_runs_the_ads_image(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_dir = tmp_path / "state"
        podman_log = tmp_path / "podman.log"
        result = subprocess.run(
            ["bash", str(_PROVISION_SH), "--scaffold"],
            env={
                **os.environ,
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "SAFENT_COMPANION_STATE": str(state_dir),
                "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
                "FAKE_PODMAN_LOG": str(podman_log),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        log = podman_log.read_text()
        assert "pull" not in log
        assert "compose" not in log
        assert "gen_keys" not in log

    def test_scaffold_writes_a_companions_json_shape_the_daemon_loader_accepts(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """Cross-boundary proof: the SAME companions.json scaffold mode
        writes has the exact shape `hermes.shell_server.companions`'s
        field-level validation (`_validate_companion_entry`) accepts —
        never a placeholder shape it would reject. The file-OWNERSHIP trust
        boundary itself (`_is_trustworthy_file`: root-owned or on a `:ro`
        bind) is a SEPARATE, already-covered concern (test_companions.py) —
        this script only ever runs as the host user, never as the
        container's root, so simulating that mount here would test the
        wrong layer.
        """
        state_dir = tmp_path / "state"
        result = subprocess.run(
            ["bash", str(_PROVISION_SH), "--scaffold"],
            env={
                **os.environ,
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "SAFENT_COMPANION_STATE": str(state_dir),
                "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
                "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

        import hashlib as _hashlib  # noqa: PLC0415
        import json as _json  # noqa: PLC0415
        import ssl as _ssl  # noqa: PLC0415

        from hermes.shell_server.companions import _validate_ip, _validate_url  # noqa: PLC0415

        doc = _json.loads((state_dir / "companions.json").read_text())
        assert doc["version"] == 1
        entry = doc["companions"][0]
        assert entry["slug"] == "safent-ads"
        assert _validate_url(entry["url"]) == "ads.safent.internal"
        assert _validate_ip(entry["ip"]) == "10.201.0.10"
        assert entry["port"] == 8443
        assert entry["ca_path"] == "/etc/hermes/companions/ads-ca.crt"
        assert entry["bearer_ref"] == "file:/etc/hermes/companions/ads.bearer"
        # ca_fingerprint is re-derived from the REAL scaffolded CA (host-side
        # path) the exact same way _validate_ca_fingerprint does against the
        # container-side mount -- proves the value is not stale/fabricated.
        pem = (state_dir / "tls" / "ca.crt").read_text()
        der = _ssl.PEM_cert_to_DER_cert(pem)
        assert entry["ca_fingerprint"] == f"sha256:{_hashlib.sha256(der).hexdigest()}"

    def test_scaffold_leaves_the_sso_key_as_an_empty_placeholder(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_dir = tmp_path / "state"
        result = subprocess.run(
            ["bash", str(_PROVISION_SH), "--scaffold"],
            env={
                **os.environ,
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "SAFENT_COMPANION_STATE": str(state_dir),
                "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
                "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        key_path = state_dir / "sso" / "ads-sso.key"
        assert key_path.read_bytes() == b""
        assert _mode(key_path) == 0o400

    def test_scaffold_generates_a_real_non_empty_bearer(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """The bearer is image-independent (openssl rand) — scaffold mode
        generates the REAL value immediately, unlike the SSO key."""
        state_dir = tmp_path / "state"
        result = subprocess.run(
            ["bash", str(_PROVISION_SH), "--scaffold"],
            env={
                **os.environ,
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "SAFENT_COMPANION_STATE": str(state_dir),
                "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
                "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        bearer = (state_dir / "bearer").read_text().strip()
        assert len(bearer) == 64  # openssl rand -hex 32
        assert _mode(state_dir / "bearer") == 0o400

    def test_a_later_full_run_generates_the_real_sso_key_over_the_placeholder(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """T016's `safent companion install` runs provision.sh WITHOUT
        --scaffold on top of an existing scaffold — the empty placeholder
        must not block the real keypair from being generated (the `-s`,
        not `-f`, check in ensure_sso_keypair)."""
        state_dir = tmp_path / "state"
        podman_log = tmp_path / "podman.log"
        env = {
            **os.environ,
            "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
            "SAFENT_COMPANION_STATE": str(state_dir),
            "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
            "FAKE_PODMAN_LOG": str(podman_log),
        }
        scaffold = subprocess.run(
            ["bash", str(_PROVISION_SH), "--scaffold"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert scaffold.returncode == 0, scaffold.stderr
        assert (state_dir / "sso" / "ads-sso.key").read_bytes() == b""

        full = subprocess.run(
            ["bash", str(_PROVISION_SH)], env=env, capture_output=True, text=True, timeout=60,
        )
        assert full.returncode == 0, full.stderr
        assert (state_dir / "sso" / "ads-sso.key").read_bytes() != b""
        assert _mode(state_dir / "sso" / "ads-sso.key") == 0o400


class TestHonoursSafentPodmanOverPath:
    """contracts/app-engine.md §1: SAFENT_PODMAN wins over PATH resolution
    — the desktop app ships its OWN pinned podman and this script must
    never fall back to whatever happens to be on PATH once invoked from
    the embedded CLI (`safent companion install|repair`, T016)."""

    def test_scaffold_uses_the_pinned_binary_never_the_one_on_path(
        self, tmp_path: Path
    ) -> None:
        # Two DISTINCT fake podmans, each logging to its own file: one on
        # PATH (as a terminal user's own install would have), one pinned
        # via SAFENT_PODMAN (as the desktop app ships). Only the pinned one
        # may ever be called. The real system PATH is kept (appended) so
        # openssl/sed/etc. still resolve normally — only podman resolution
        # itself is under test.
        # Each fake writes to a path baked directly into ITS OWN script —
        # never the shared FAKE_PODMAN_LOG env var — so calling the WRONG
        # binary is observable regardless of what env either one sees.
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        path_podman_log = tmp_path / "path-podman.log"
        path_podman = bin_dir / "podman"
        path_podman.write_text(
            f'#!/usr/bin/env bash\necho "$@" >> {path_podman_log}\nexit 0\n'
        )
        path_podman.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(_FAKE_CURL)
        curl.chmod(0o755)

        pinned_dir = tmp_path / "pinned"
        pinned_dir.mkdir()
        pinned_podman_log = tmp_path / "pinned-podman.log"
        pinned_podman = pinned_dir / "podman"
        pinned_podman.write_text(
            f'#!/usr/bin/env bash\necho "$@" >> {pinned_podman_log}\n'
            'case "$1 $2" in\n'
            '  "network inspect") echo "10.201.0.0/24"; exit 0 ;;\n'
            '  "network create") exit 0 ;;\n'
            "esac\n"
            "exit 0\n"
        )
        pinned_podman.chmod(0o755)

        state_dir = tmp_path / "state"
        result = subprocess.run(
            ["bash", str(_PROVISION_SH), "--scaffold"],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "SAFENT_PODMAN": str(pinned_podman),
                "SAFENT_COMPANION_STATE": str(state_dir),
                "SAFENT_ADS_IMAGE": "safent-ads:test-fake",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert pinned_podman_log.exists(), "the pinned SAFENT_PODMAN binary was never invoked"
        assert not path_podman_log.exists(), "podman on PATH was called despite SAFENT_PODMAN being set"
        assert (state_dir / "companions.json").exists()


class TestComposeConfigRenders:
    """`podman compose config` / `docker compose config` with a dummy state
    — proves compose.yaml's variable interpolation and bind mounts resolve
    (not that the services actually boot, out of scope for a unit test)."""

    def test_compose_config_renders_with_dummy_state(self, tmp_path: Path) -> None:
        engine = shutil.which("podman") or shutil.which("docker")
        if engine is None:
            pytest.skip("neither podman nor docker is on PATH")
        probe = subprocess.run(
            [engine, "compose", "version"], capture_output=True, text=True, timeout=20
        )
        if probe.returncode != 0:
            pytest.skip(f"{engine} has no usable compose plugin")

        state_dir = tmp_path / "state"
        (state_dir / "tls").mkdir(parents=True)
        (state_dir / "tls" / "leaf.crt").write_text("dummy-cert")
        (state_dir / "tls" / "leaf.key").write_text("dummy-key")
        (state_dir / "secrets").mkdir()
        (state_dir / "secrets" / "api.env").write_text("ADS_MCP_TOKEN=dummy\n")
        (state_dir / "secrets" / "broker.env").write_text("ADS_APPROVAL_PUBLIC_KEY=dummy\n")
        shutil.copy(_CAPS_TEMPLATE, state_dir / "caps.yaml")

        env = dict(os.environ)
        env["SAFENT_STATE"] = str(state_dir)
        env["ADS_POSTGRES_PASSWORD"] = "x"
        env["SAFENT_ADS_IMAGE"] = "safent-ads:local"

        result = subprocess.run(
            [engine, "compose", "-f", str(_COMPOSE_YAML), "-p", "safent-ads", "config"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "10.201.0.10" in result.stdout
        assert "ADS_COMPANION_MODE" in result.stdout


# =============================================================================
# T193 — `safent companion status|update|rotate|remove`
#
# Before this, `safent` had no dedicated companion lifecycle: `safent update`
# re-provisioned it as a SIDE EFFECT of recreating the Safent container, and
# `safent start` on an existing container did not even do that (runbook.md
# §9). These tests drive the REAL `safent` script (sh, not sourced) with only
# podman/curl faked — same "fake the two commands that would touch a real
# engine" discipline as TestFirstRunWritesExpectedFiles above.
# =============================================================================

_SAFENT_CLI = _REPO_ROOT / "safent"

# Args land on this fake exactly as the CLI invokes them:
#   network inspect safent-companions
#   network rm safent-companions
#   pull <image>
#   run --rm --network none <image> python -m safent_ads.tools.gen_keys
#   compose -p safent-ads -f <compose> ps -q -a
#   compose -p safent-ads -f <compose> up -d [--force-recreate ads-api]
#   compose -p safent-ads -f <compose> down
#   inspect -f {{.State.Running}} <id>
_FAKE_PODMAN_CLI = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"
case "$1" in
  network)
    case "$2" in
      inspect) [ "${FAKE_NETWORK_PRESENT:-1}" = "1" ] && exit 0 || exit 1 ;;
      rm) exit 0 ;;
    esac
    ;;
  pull)
    [ "${FAKE_PULL_FAIL:-0}" = "1" ] && exit 1
    exit 0
    ;;
  run)
    [ "${FAKE_GEN_KEYS_FAIL:-0}" = "1" ] && exit 1
    for a in "$@"; do
      if [ "$a" = "safent_ads.tools.gen_keys" ]; then
        echo "ADS_APPROVAL_SIGNING_KEY=new-sso-seed-$RANDOM$RANDOM"
        echo "ADS_APPROVAL_PUBLIC_KEY=new+sso/pub-$RANDOM$RANDOM=="
        exit 0
      fi
      if [ "$a" = "alembic" ]; then
        # CLI-10 migration-head guard: `$image alembic history` — args are
        # `run --rm --network none <image> alembic history`, image is $5.
        printf '%s\n' "${FAKE_ALEMBIC_HISTORY:-}"
        exit 0
      fi
    done
    exit 0
    ;;
  compose)
    verb="$6"
    case "$verb" in
      ps)
        # CLI-08: the REAL compose.yaml interpolates
        # ${ADS_POSTGRES_PASSWORD:?required} — a caller that forgot to
        # export it gets an interpolation error and an EMPTY `ps -q -a`,
        # not the container list. Model that exact failure instead of
        # ignoring the env entirely (a fake that always answers regardless
        # of env would never catch _companion_container_counts calling
        # compose WITHOUT `_companion_env` first).
        if [ -z "${ADS_POSTGRES_PASSWORD:-}" ]; then
          echo "required variable ADS_POSTGRES_PASSWORD is missing a value" >&2
          exit 0  # `|| true` in the CLI swallows this; ids stays empty either way
        fi
        for id in ${FAKE_COMPOSE_IDS:-c1 c2}; do echo "$id"; done
        exit 0
        ;;
      up) [ "${FAKE_COMPOSE_UP_FAIL:-0}" = "1" ] && exit 1; exit 0 ;;
      down) exit 0 ;;
      exec)
        # CLI-10 migration-head guard's DB read: `exec -T ads-db psql -U ads
        # -d ads -tAc 'SELECT version_num FROM alembic_version;'`. Empty by
        # default (no FAKE_DB_REVISION) — matches the "brand new DB, skip
        # the guard" path every pre-existing test in this file relies on.
        printf '%s\n' "${FAKE_DB_REVISION:-}"
        exit 0
        ;;
    esac
    exit 0
    ;;
  inspect)
    id="$4"
    case " ${FAKE_RUNNING_IDS:-c1 c2} " in
      *" $id "*) echo true ;;
      *) echo false ;;
    esac
    exit 0
    ;;
esac
exit 0
"""

_FAKE_CURL_HEALTH = """#!/usr/bin/env bash
printf '%s' "${FAKE_HEALTH_CODE:-401}"
exit 0
"""


@pytest.fixture()
def fake_cli_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(_FAKE_PODMAN_CLI)
    podman.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(_FAKE_CURL_HEALTH)
    curl.chmod(0o755)
    return bin_dir


def _companion_state(tmp_path: Path, *, provisioned: bool, with_image_marker: bool = True) -> Path:
    """A minimal $COMPANION_STATE — only what the CLI's own verbs read
    (bearer, secrets/api.env, tls/ca.crt, sso/ads-sso.key, image), never
    provision.sh's full output.

    with_image_marker=False models a companion missing $STATE/image (a
    companion provisioned before CLI-10's fix, or the marker deleted/lost)
    — security review 2026-09-10 (MEDIUM finding): status/rotate/remove
    must now refuse rather than guess a hard-coded default in that case."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if provisioned:
        (state_dir / "tls").mkdir()
        (state_dir / "tls" / "ca.crt").write_text("dummy-ca")
        (state_dir / "secrets").mkdir()
        (state_dir / "secrets" / "api.env").write_text(
            "ADS_MCP_TOKEN=old-bearer-value\n"
            "ADS_SESSION_SECRET=x\n"
            "ADS_SSO_PUBLIC_KEY=old-sso-pub\n"
        )
        (state_dir / "bearer").write_text("old-bearer-value\n")
        (state_dir / "bearer").chmod(0o400)
        (state_dir / "sso").mkdir()
        (state_dir / "sso" / "ads-sso.key").write_text("old-sso-seed\n")
        (state_dir / "sso" / "ads-sso.key").chmod(0o400)
        if with_image_marker:
            (state_dir / "image").write_text("safent-ads:test-fake")
    return state_dir


def _companion_bin_dir(home_dir: Path, *, provisioned: bool) -> Path:
    """$COMPANION_BIN_DIR derives from $HOME (safent has no override env for
    it) — mirrors run-safent.sh/provision.sh's own cached-file layout."""
    bin_dir = home_dir / ".safent" / "companions" / "ads" / "bin"
    bin_dir.mkdir(parents=True)
    if provisioned:
        shutil.copy(_COMPOSE_YAML, bin_dir / "compose.yaml")
    return bin_dir


def _run_companion(
    *args: str,
    fake_bin_dir: Path,
    state_dir: Path,
    home_dir: Path,
    podman_log: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"
    env["HOME"] = str(home_dir)
    env["SAFENT_COMPANION_STATE"] = str(state_dir)
    env["SAFENT_ADS_IMAGE"] = "safent-ads:test-fake"
    env["FAKE_PODMAN_LOG"] = str(podman_log)
    env.update(extra_env or {})
    return subprocess.run(
        ["sh", str(_SAFENT_CLI), "companion", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCompanionStatus:
    def test_reports_not_provisioned_when_never_provisioned(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=False)
        result = _run_companion(
            "status",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode == 0, result.stderr
        assert "not provisioned" in result.stdout

    def test_reports_network_containers_and_health_when_provisioned(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "status",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
            extra_env={
                "FAKE_NETWORK_PRESENT": "1",
                "FAKE_COMPOSE_IDS": "c1 c2 c3",
                "FAKE_RUNNING_IDS": "c1 c2",
                "FAKE_HEALTH_CODE": "401",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "network:      up" in result.stdout
        assert "containers:   2/3 running" in result.stdout
        assert "/mcp/health:  reachable (HTTP 401)" in result.stdout

    def test_container_counts_are_not_zero_even_without_a_preexported_password(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """CLI-08 root cause, reproduced exactly: `_run_companion` (this
        file's own harness, like a real shell) never exports
        ADS_POSTGRES_PASSWORD — `_companion_container_counts` MUST call
        `_companion_env` itself before invoking compose, or the fake's `ps`
        branch (modelling compose.yaml's real `${ADS_POSTGRES_PASSWORD:?...}`
        interpolation failure) returns no container IDs at all, exactly the
        `0/0 running` the matrix row reported against a companion whose 5
        containers were actually Up/healthy."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "status",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
            extra_env={
                "FAKE_NETWORK_PRESENT": "1",
                "FAKE_COMPOSE_IDS": "c1 c2 c3 c4 c5",
                "FAKE_RUNNING_IDS": "c1 c2 c3 c4 c5",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "containers:   0/0 running" not in result.stdout, result.stdout
        assert "containers:   5/5 running" in result.stdout

    def test_reports_network_absent(self, tmp_path: Path, fake_cli_bin_dir: Path) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "status",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
            extra_env={"FAKE_NETWORK_PRESENT": "0"},
        )
        assert result.returncode == 0, result.stderr
        assert "network:      absent" in result.stdout


class TestCompanionUpdate:
    def test_pulls_the_image_and_recreates_the_containers(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
        )
        assert result.returncode == 0, result.stderr
        log = podman_log.read_text()
        assert "pull safent-ads:test-fake" in log
        assert "up -d" in log

    def test_persists_the_image_marker_after_a_successful_update(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """`update` is the one verb allowed to CHANGE $STATE/image (CLI-10);
        status/rotate/remove read it back via _companion_env."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode == 0, result.stderr
        assert (state_dir / "image").read_text() == "safent-ads:test-fake"

    def test_fails_loud_when_not_provisioned(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=False)
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "not provisioned" in result.stderr

    def test_fails_loud_when_pull_fails(self, tmp_path: Path, fake_cli_bin_dir: Path) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
            extra_env={"FAKE_PULL_FAIL": "1"},
        )
        assert result.returncode != 0
        assert "Could not pull" in result.stderr


class TestCompanionUpdateMigrationGuard:
    """CLI-10: `ads-migrate` (`alembic upgrade head`) died loud
    (`Can't locate revision identified by '0033_crm_bridge_health'`, exit
    255) against the real companion when `update`'s image was older than
    what the database had already migrated to — but only AFTER ads-api/
    ads-worker were already recreated against it, leaving ads-api down.
    `_refuse_if_image_predates_the_database` reads the target image's own
    `alembic history` (no DB access needed) and the database's current
    `alembic_version` row, refusing BEFORE `up -d` if the image never heard
    of that revision."""

    def test_refuses_when_the_image_does_not_know_the_databases_revision(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={
                "FAKE_DB_REVISION": "0033_crm_bridge_health",
                "FAKE_ALEMBIC_HISTORY": "0001_init -> 0002_accounts, add accounts table",
            },
        )
        assert result.returncode != 0
        assert "0033_crm_bridge_health" in result.stderr
        assert "OLDER than the database" in result.stderr
        # The refusal must be BEFORE recreating anything — no `up -d` issued.
        log = podman_log.read_text()
        assert "up -d" not in log

    def test_proceeds_when_the_image_knows_the_databases_revision(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={
                "FAKE_DB_REVISION": "0033_crm_bridge_health",
                "FAKE_ALEMBIC_HISTORY": "0032_x -> 0033_crm_bridge_health, add health cols",
            },
        )
        assert result.returncode == 0, result.stderr
        assert (state_dir / "image").read_text() == "safent-ads:test-fake"

    def test_a_brand_new_database_skips_the_guard(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """No alembic_version row yet (fresh DB) is not "older" — it just
        has not been migrated yet; ads-migrate will populate it."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode == 0, result.stderr

    def test_unreadable_history_refuses_instead_of_proceeding_when_db_rev_is_known(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """Security review 2026-09-10 (MEDIUM finding, CWE-754): this used
        to fail OPEN — an unreadable history "proceeded without the guard".
        Once db_rev is known (the DB has been migrated), an unreadable
        history for the TARGET image is now a refusal, not a shrug: we
        cannot prove the image is safe, and the downside of a wrong guess
        (ads-api down) is exactly what this guard exists to prevent."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={"FAKE_DB_REVISION": "0033_crm_bridge_health"},  # no FAKE_ALEMBIC_HISTORY
        )
        assert result.returncode != 0
        assert "refusing to update against an unverifiable image" in result.stderr
        log = podman_log.read_text()
        assert "up -d" not in log

    def test_match_is_word_bounded_not_a_bare_substring(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """Security review 2026-09-10 (MEDIUM finding): `case "$history" in
        *"$db_rev"*)` matched a db_rev that merely APPEARED inside an
        unrelated line — e.g. as a substring of a longer revision id or a
        commit message. `0033` must not be satisfied by a history that only
        mentions `00337_unrelated`."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
            extra_env={
                "FAKE_DB_REVISION": "0033",
                "FAKE_ALEMBIC_HISTORY": "0032_x -> 00337_unrelated, an unrelated later revision",
            },
        )
        assert result.returncode != 0
        assert "does not know revision '0033'" in result.stderr

    def test_rotate_is_guarded_too(self, tmp_path: Path, fake_cli_bin_dir: Path) -> None:
        """Security review 2026-09-10 (MEDIUM finding): the guard used to
        sit ONLY in `update` — `rotate` recreates ads-api via
        --force-recreate just the same and needs the same protection."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "rotate",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={
                "FAKE_DB_REVISION": "0033_crm_bridge_health",
                "FAKE_ALEMBIC_HISTORY": "0001_init -> 0002_accounts, add accounts table",
            },
        )
        assert result.returncode != 0
        assert "OLDER than the database" in result.stderr
        log = podman_log.read_text()
        assert "--force-recreate" not in log


class TestMissingImageMarkerFailsClosed:
    """Security review 2026-09-10 (MEDIUM finding, CWE-754): `_persisted_
    ads_image` used to fall back to the hard-coded ghcr.io/…/safent-ads:
    latest default when $STATE/image was absent — deleting that ONE file
    (0700 dir, no integrity protection) silently restored the exact CLI-10
    divergence this whole fix set out to close. status/rotate/remove must
    now refuse with a clear recovery step instead of guessing; `update`
    alone keeps the historical fallback (it is the one verb allowed to
    CHOOSE an image, so falling back to the published default there is a
    deliberate bootstrap, not a guess)."""

    def test_status_refuses_without_the_marker(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True, with_image_marker=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "status",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "refusing to guess the companion's image" in result.stderr
        assert "safent companion update" in result.stderr

    def test_rotate_refuses_without_the_marker(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True, with_image_marker=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "rotate",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
        )
        assert result.returncode != 0
        assert "refusing to guess the companion's image" in result.stderr
        # Refuses BEFORE rotating anything — no bearer/SSO files touched.
        assert "old-bearer-value" == (state_dir / "bearer").read_text().strip()

    def test_remove_refuses_without_the_marker(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True, with_image_marker=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "remove",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "refusing to guess the companion's image" in result.stderr

    def test_update_still_bootstraps_with_the_published_default(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """The one deliberate exception — update is ALLOWED to guess,
        because guessing is the whole point of this verb: it always PICKS
        an image (explicit override or the published default) and then
        PERSISTS its choice, closing the gap for every future verb."""
        state_dir = _companion_state(tmp_path, provisioned=True, with_image_marker=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"
        result = _run_companion(
            "update",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={"SAFENT_ADS_IMAGE": ""},  # no explicit override either
        )
        assert result.returncode == 0, result.stderr
        assert (state_dir / "image").read_text() == "ghcr.io/devwspito/safent-ads:latest"
        log = podman_log.read_text()
        assert "pull ghcr.io/devwspito/safent-ads:latest" in log


class TestCompanionRotate:
    def test_rotates_bearer_on_both_sides_and_restarts_ads_api(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        old_bearer = (state_dir / "bearer").read_text().strip()
        podman_log = tmp_path / "podman.log"

        result = _run_companion(
            "rotate",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
        )

        assert result.returncode == 0, result.stderr
        new_bearer = (state_dir / "bearer").read_text().strip()
        assert new_bearer != old_bearer
        assert len(new_bearer) == 64  # openssl rand -hex 32
        assert stat.S_IMODE((state_dir / "bearer").stat().st_mode) == 0o400

        api_env = (state_dir / "secrets" / "api.env").read_text()
        assert f"ADS_MCP_TOKEN={new_bearer}" in api_env
        assert "ADS_SESSION_SECRET=x" in api_env  # every other line survives
        assert api_env.count("ADS_MCP_TOKEN=") == 1

        # 026 — the SSO Ed25519 pair rotates alongside the bearer.
        new_sso_seed = (state_dir / "sso" / "ads-sso.key").read_text().strip()
        assert new_sso_seed != "old-sso-seed"
        assert stat.S_IMODE((state_dir / "sso" / "ads-sso.key").stat().st_mode) == 0o400
        assert "ADS_SSO_PUBLIC_KEY=old-sso-pub" not in api_env
        assert api_env.count("ADS_SSO_PUBLIC_KEY=") == 1
        new_sso_pub = next(
            ln.split("=", 1)[1]
            for ln in api_env.splitlines()
            if ln.startswith("ADS_SSO_PUBLIC_KEY=")
        )
        assert "+" not in new_sso_pub and "/" not in new_sso_pub  # url-safe b64

        log = podman_log.read_text()
        assert "run --rm --network none" in log
        assert "up -d --force-recreate ads-api" in log
        assert "restart safent" in result.stdout.lower()

    def test_rotate_uses_the_persisted_image_not_a_stray_ambient_env_var(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        """CLI-10 root cause, reproduced exactly: a companion provisioned
        with `safent-ads:local` (persisted to $STATE/image by provision.sh)
        must have `rotate` reuse THAT image — never a stray SAFENT_ADS_IMAGE
        left over in the caller's shell (here, `_run_companion` itself
        always sets one, standing in for exactly that stray-env shape) and
        never the historical ghcr.io/…/safent-ads:latest default. Mixing
        images is what made `ads-migrate` die `Can't locate revision …`
        against the real companion."""
        state_dir = _companion_state(tmp_path, provisioned=True)
        (state_dir / "image").write_text("localhost/safent-ads:persisted-v2")
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"

        result = _run_companion(
            "rotate",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={"SAFENT_ADS_IMAGE": "ghcr.io/devwspito/safent-ads:latest"},
        )

        assert result.returncode == 0, result.stderr
        log = podman_log.read_text()
        assert "localhost/safent-ads:persisted-v2" in log
        assert "ghcr.io/devwspito/safent-ads:latest" not in log

    def test_fails_loud_when_sso_keygen_fails(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        old_bearer = (state_dir / "bearer").read_text().strip()

        result = _run_companion(
            "rotate",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
            extra_env={"FAKE_GEN_KEYS_FAIL": "1"},
        )

        assert result.returncode != 0
        # Bearer rotation already committed before the SSO step — the old
        # SSO key is left untouched rather than half-rotated.
        assert (state_dir / "bearer").read_text().strip() != old_bearer
        assert (state_dir / "sso" / "ads-sso.key").read_text().strip() == "old-sso-seed"

    def test_fails_loud_when_secrets_are_missing(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        (state_dir / "secrets" / "api.env").unlink()
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        result = _run_companion(
            "rotate",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "not found" in result.stderr


class TestCompanionRemove:
    def test_composes_down_and_removes_the_network_keeping_state(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)
        podman_log = tmp_path / "podman.log"

        result = _run_companion(
            "remove",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=podman_log,
        )

        assert result.returncode == 0, result.stderr
        log = podman_log.read_text()
        assert "compose -p safent-ads" in log and "down" in log
        assert "network rm safent-companions" in log
        assert state_dir.exists()  # state survives without --purge
        assert (state_dir / "secrets" / "api.env").exists()

    def test_purge_also_deletes_state(self, tmp_path: Path, fake_cli_bin_dir: Path) -> None:
        state_dir = _companion_state(tmp_path, provisioned=True)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=True)

        result = _run_companion(
            "remove",
            "--purge",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )

        assert result.returncode == 0, result.stderr
        assert not state_dir.exists()


class TestImageRefNeverReachesPodmanRunInOptionPosition:
    """Security review 2026-09-10 (LOW finding, CWE-88): the persisted/
    resolved image ref is quoted (no word-splitting) but sits where
    `podman run` still accepts options — a value beginning with `-` would
    be consumed as a flag, not an image name. `$STATE` is 0700 owner-only
    (no privilege boundary crossed today, per the review's own read), but
    `--` costs nothing and removes the shape entirely. Static check: every
    `run --rm --network none` invocation of an image variable in both
    scripts must have `--` immediately before it."""

    def test_safent_cli(self) -> None:
        src = _SAFENT_CLI.read_text(encoding="utf-8")
        for line in src.splitlines():
            if "run --rm --network none" in line:
                assert "run --rm --network none -- " in line, line

    def test_provision_sh(self) -> None:
        src = _PROVISION_SH.read_text(encoding="utf-8")
        for line in src.splitlines():
            if "run --rm --network none" in line:
                assert "run --rm --network none -- " in line, line


class TestCompanionUsageGuard:
    def test_unknown_verb_fails_loud_with_usage(
        self, tmp_path: Path, fake_cli_bin_dir: Path
    ) -> None:
        state_dir = _companion_state(tmp_path, provisioned=False)
        home_dir = tmp_path / "home"
        _companion_bin_dir(home_dir, provisioned=False)
        result = _run_companion(
            "bogus",
            fake_bin_dir=fake_cli_bin_dir,
            state_dir=state_dir,
            home_dir=home_dir,
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "Usage: safent companion" in result.stderr
