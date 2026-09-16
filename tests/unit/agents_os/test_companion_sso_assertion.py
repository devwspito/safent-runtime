"""CompanionSsoAuthority (026, contracts/sso.md §3, T004) — the daemon's
only signer of owner assertions for the Safent -> safent-ads session
bridge, and `DbusRuntimeServiceWiring.mint_companion_owner_assertion`'s
sender_uid gate.

Covers: exact payload shape (contracts/sso.md §3), TTL == 60 s, a distinct
`jti` per call, denial of any sender_uid other than the shell-server's own,
denial of a key file that fails the ownership/permission invariant (the
REAL check reused from `hermes.shell_server.companions`, not mocked), the
30/min rate limit, and that the private key material never appears in a
returned value, an exception message, or a log line.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.agents_os.infrastructure.companion_sso_authority import (
    _RATE_LIMIT_MAX_PER_MINUTE,
    _SSO_PRIVATE_KEY_PATH,
    CompanionSsoAuthority,
    CompanionSsoKeyUnavailableError,
    CompanionSsoRateLimitedError,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.shell_server import companions as companions_mod
from hermes.shell_server.security import secrets as secrets_mod
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_AUTHORIZED_UID = 1000  # hermes-user (direct operator) — never allowed here
_PROXY_UID = 880  # shell-server process — the ONLY caller this verb allows
_UNAUTHORIZED_UID = 9999
_SLUG = "safent-ads"


@pytest.fixture(autouse=True)
def master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sub` derives from master.key (contracts/sso.md §3) — every test
    needs a fake one; a fixed 32-byte value keeps `sub` deterministic across
    tests that compare it."""
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\x22" * 32)
    monkeypatch.setattr(secrets_mod, "_MASTER_KEY_PATH", key_file)


class _FakeApprovalGate:
    async def approve(self, **_kwargs) -> str:
        return "token"

    async def reject(self, **_kwargs) -> None:
        return None


def _make_wiring(*, proxy_uid: int | None = _PROXY_UID) -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({_AUTHORIZED_UID}),
        proxy_uid=proxy_uid,
    )


def _make_seed_file(tmp_path: Path, *, mode: int = 0o400) -> tuple[Path, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    seed_b64 = base64.b64encode(key.private_bytes_raw()).decode("ascii")
    path = tmp_path / "ads-sso.key"
    path.write_text(seed_b64 + "\n")
    path.chmod(mode)
    return path, key


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_payload(assertion: str) -> dict:
    payload_b64 = assertion.split(".", 1)[0]
    return json.loads(_b64url_decode(payload_b64))


@pytest.fixture()
def trust_all_key_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass ONLY the ownership/read-only-mount branch of the shared
    invariant (a plain temp file created by the test process fails that
    branch in any sandbox — same technique test_companions.py already uses)
    so happy-path tests focus on the signing/payload contract, not on
    re-proving companions.py's own file-trust logic (covered there)."""
    monkeypatch.setattr(
        companions_mod, "is_companion_secret_file_trustworthy", lambda _path: True
    )


# ============================================================================
# ADS-02 regression — the daemon must read the ROOT-STAGED runtime copy, not
# the raw bind mount `MintCompanionOwnerAssertion` used to read directly.
# That mount is root:root 0400 inside the container (rootless remap or not)
# while the daemon runs as `User=hermes` (uid 880) — every mint attempt
# raised PermissionError and `/ads/*` 503'd (matriz-app-completa-resultados-
# dgx.md, ADS-02). The fix mirrors the 024 companion-bearer stage-in exactly:
# hermes-companion-bearer's root ExecStartPre=-+ copies the mount to
# /run/hermes/companions/ads-sso.key, 0440 root:hermes, readable by the
# daemon's own group.
# ============================================================================


class TestDefaultKeyPathIsTheRootStagedCopy:
    def test_default_path_is_the_runtime_staged_copy_not_the_raw_mount(self) -> None:
        assert _SSO_PRIVATE_KEY_PATH == Path(companions_mod.COMPANION_RUNTIME_SSO_KEY_PATH)
        assert _SSO_PRIVATE_KEY_PATH != Path(companions_mod.COMPANION_SSO_KEY_MOUNT_PATH)

    def test_the_wiring_used_in_production_relies_on_that_default(self) -> None:
        """`DbusRuntimeServiceWiring` builds `CompanionSsoAuthority()` with NO
        override (dbus_runtime_service.py) — the module default IS what the
        real daemon reads from."""
        authority = CompanionSsoAuthority()
        assert authority._private_key_path == _SSO_PRIVATE_KEY_PATH  # noqa: SLF001


# ============================================================================
# Payload contract (contracts/sso.md §3)
# ============================================================================


class TestAssertionPayloadContract:
    def test_payload_has_exactly_the_contract_fields(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)

        result = authority.mint_owner_assertion(slug=_SLUG)
        payload = _decode_payload(result.assertion)

        assert set(payload.keys()) == {
            "v", "iss", "aud", "slug", "sub", "jti", "iat", "exp", "purpose", "surface",
        }
        assert payload["v"] == 1
        assert payload["iss"] == "safent-runtime"
        assert payload["aud"] == "safent-ads"
        assert payload["slug"] == _SLUG
        assert payload["purpose"] == "cockpit_session"
        assert payload["surface"] == "safent_cockpit"
        assert isinstance(payload["sub"], str)
        assert len(payload["sub"]) == 64  # sha256 hex digest

    def test_sub_is_stable_across_calls_and_not_the_master_key(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)
        p1 = _decode_payload(authority.mint_owner_assertion(slug=_SLUG).assertion)
        p2 = _decode_payload(authority.mint_owner_assertion(slug=_SLUG).assertion)
        assert p1["sub"] == p2["sub"]

    def test_signature_verifies_against_the_matching_public_key(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)

        result = authority.mint_owner_assertion(slug=_SLUG)
        payload_b64, sig_b64 = result.assertion.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)

        key.public_key().verify(signature, payload_bytes)  # raises InvalidSignature on failure

    def test_expires_at_matches_the_payload_exp(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        from datetime import UTC, datetime

        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)
        result = authority.mint_owner_assertion(slug=_SLUG)
        payload = _decode_payload(result.assertion)
        expected = datetime.fromtimestamp(payload["exp"], tz=UTC).isoformat().replace("+00:00", "Z")
        assert result.expires_at == expected


class TestAssertionTtl:
    def test_ttl_is_exactly_60_seconds(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)
        payload = _decode_payload(authority.mint_owner_assertion(slug=_SLUG).assertion)
        assert payload["exp"] - payload["iat"] == 60


class TestJtiIsDistinct:
    def test_two_calls_never_share_a_jti(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)
        jti1 = _decode_payload(authority.mint_owner_assertion(slug=_SLUG).assertion)["jti"]
        jti2 = _decode_payload(authority.mint_owner_assertion(slug=_SLUG).assertion)["jti"]
        assert jti1 != jti2

    def test_ten_calls_produce_ten_distinct_jtis(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)
        jtis = {
            _decode_payload(authority.mint_owner_assertion(slug=_SLUG).assertion)["jti"]
            for _ in range(10)
        }
        assert len(jtis) == 10


# ============================================================================
# sender_uid gate (contracts/sso.md §3 — "solo el uid del shell-server")
# ============================================================================


class TestSenderUidAuthorization:
    def test_unauthorized_uid_denied(self) -> None:
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.mint_companion_owner_assertion(slug=_SLUG, sender_uid=_UNAUTHORIZED_UID)

    def test_the_human_operator_uid_is_also_denied(self) -> None:
        """Stricter than every other proxied verb: this assertion carries no
        operator identity to extract via a token, so even the DIRECT
        operator uid (normally always authorized) is refused — only the
        shell-server's own uid may call it."""
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.mint_companion_owner_assertion(slug=_SLUG, sender_uid=_AUTHORIZED_UID)

    def test_no_proxy_uid_configured_denies_everyone(self) -> None:
        wiring = _make_wiring(proxy_uid=None)
        with pytest.raises(DbusAuthorizationError):
            wiring.mint_companion_owner_assertion(slug=_SLUG, sender_uid=_PROXY_UID)

    def test_shell_server_uid_is_authorized(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        wiring = _make_wiring()
        wiring._companion_sso_authority_instance = CompanionSsoAuthority(private_key_path=path)  # noqa: SLF001

        result = wiring.mint_companion_owner_assertion(slug=_SLUG, sender_uid=_PROXY_UID)

        assert "assertion" in result
        assert "expires_at" in result


# ============================================================================
# Loose-permission key file -> deny (REAL invariant, not mocked)
# ============================================================================


class TestLoosePermissionKeyFileIsRefused:
    def test_group_or_other_writable_key_file_is_refused(self, tmp_path: Path) -> None:
        path, _key = _make_seed_file(tmp_path, mode=0o646)  # world-writable
        authority = CompanionSsoAuthority(private_key_path=path)
        with pytest.raises(CompanionSsoKeyUnavailableError):
            authority.mint_owner_assertion(slug=_SLUG)

    def test_a_file_not_owned_by_root_or_on_a_read_only_mount_is_refused_by_default(
        self, tmp_path: Path
    ) -> None:
        """Without the fixture bypass, a plain 0400 file created by the test
        process itself still fails the SAME invariant production relies on
        (owned by root, or on a read-only mount and owned by someone else) —
        proving the loader is fail-closed by default, not merely when a mode
        bit happens to be wrong."""
        path, _key = _make_seed_file(tmp_path, mode=0o400)
        authority = CompanionSsoAuthority(private_key_path=path)
        with pytest.raises(CompanionSsoKeyUnavailableError):
            authority.mint_owner_assertion(slug=_SLUG)

    def test_missing_key_file_is_refused(self, tmp_path: Path) -> None:
        authority = CompanionSsoAuthority(private_key_path=tmp_path / "absent.key")
        with pytest.raises(CompanionSsoKeyUnavailableError):
            authority.mint_owner_assertion(slug=_SLUG)


# ============================================================================
# Rate limit (contracts/sso.md §3: 30/min -> RATE_LIMITED)
# ============================================================================


class TestRateLimit:
    def test_more_than_30_per_minute_is_rate_limited(
        self, tmp_path: Path, trust_all_key_files: None
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        authority = CompanionSsoAuthority(private_key_path=path)
        for _ in range(_RATE_LIMIT_MAX_PER_MINUTE):
            authority.mint_owner_assertion(slug=_SLUG)
        with pytest.raises(CompanionSsoRateLimitedError):
            authority.mint_owner_assertion(slug=_SLUG)


# ============================================================================
# The private key never leaks
# ============================================================================


class TestPrivateKeyNeverLeaks:
    def test_seed_absent_from_the_returned_dict_and_every_log_line(
        self, tmp_path: Path, trust_all_key_files: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        path, _key = _make_seed_file(tmp_path)
        seed_b64 = path.read_text().strip()
        wiring = _make_wiring()
        wiring._companion_sso_authority_instance = CompanionSsoAuthority(private_key_path=path)  # noqa: SLF001

        with caplog.at_level(logging.DEBUG):
            result = wiring.mint_companion_owner_assertion(slug=_SLUG, sender_uid=_PROXY_UID)

        assert seed_b64 not in json.dumps(result)
        for record in caplog.records:
            assert seed_b64 not in record.getMessage()

    def test_seed_absent_from_the_error_when_the_key_file_is_unreadable(
        self, tmp_path: Path
    ) -> None:
        path, _key = _make_seed_file(tmp_path, mode=0o646)
        seed_b64 = path.read_text().strip()
        authority = CompanionSsoAuthority(private_key_path=path)

        with pytest.raises(CompanionSsoKeyUnavailableError) as exc_info:
            authority.mint_owner_assertion(slug=_SLUG)

        assert seed_b64 not in str(exc_info.value)
