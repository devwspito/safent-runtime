"""Real TLS and crypto tests for the fixed-purpose local credential boundary."""

from __future__ import annotations

import base64
import json
import sqlite3
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

from hermes.agents_os.infrastructure import companion_composio_publisher as mod
from hermes.agents_os.infrastructure.companion_sso_authority import CompanionSsoAuthority
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.shell_server import companions
from hermes.shell_server.ads_bridge import _classify_path
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security import secrets
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _open(envelope, recipient, signer):
    wire = json.loads(_decode(envelope))
    assert set(wire) == {"v", "ephemeral_public_key", "nonce", "ciphertext"}
    shared = recipient.exchange(
        X25519PublicKey.from_public_bytes(_decode(wire["ephemeral_public_key"]))
    )
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"safent-composio-lease-v1"
    ).derive(shared)
    token = AESGCM(key).decrypt(
        _decode(wire["nonce"]), _decode(wire["ciphertext"]), b"safent-ads-broker:composio-config:v1"
    )
    payload, signature = token.decode().split(".")
    signer.verify(_decode(signature), _decode(payload))
    return json.loads(_decode(payload))


@pytest_asyncio.fixture
async def channel(tmp_path, monkeypatch):  # noqa: PLR0915 - one real TLS companion fixture
    key_path = tmp_path / "master.key"
    key_path.write_bytes(b"k" * 32)
    monkeypatch.setattr(secrets, "_MASTER_KEY_PATH", key_path)
    monkeypatch.setattr(companions, "is_companion_secret_file_trustworthy", lambda _: True)
    signing_key = Ed25519PrivateKey.generate()
    sso_path = tmp_path / "sso.key"
    sso_path.write_text(base64.b64encode(signing_key.private_bytes_raw()).decode())

    tls_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ads.safent.internal")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("ads.safent.internal")]), critical=False
        )
        .sign(tls_key, hashes.SHA256())
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    tls_path = tmp_path / "tls.key"
    tls_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(ca, tls_path)

    state = SimpleNamespace(
        recipient=X25519PrivateKey.generate(),
        signing_key=signing_key,
        claims=[],
        envelopes=[],
        exchange_count=0,
        channel_count=0,
        reject_session_once=False,
        redirect=False,
    )
    state.pin_path = tmp_path / "ads-composio-channel.pub"
    state.pin_path.write_text(
        base64.b64encode(state.recipient.public_key().public_bytes_raw()).decode() + "\n"
    )
    monkeypatch.setattr(mod, "_RECIPIENT_PIN_PATH", state.pin_path)

    async def exchange(request):
        state.exchange_count += 1
        assertion = (await request.json())["assertion"]
        payload, signature = assertion.split(".")
        state.signing_key.public_key().verify(_decode(signature), _decode(payload))
        assert json.loads(_decode(payload))["purpose"] == "cockpit_session"
        response = web.json_response({"ok": True})
        response.set_cookie("ads_session", "test-session", max_age=3600)
        return response

    async def get_channel(request):
        state.channel_count += 1
        assert request.cookies.get("ads_session") == "test-session"
        if state.redirect:
            return web.Response(status=307, headers={"Location": "/untrusted"})
        if state.reject_session_once:
            state.reject_session_once = False
            return web.Response(status=401)
        response = web.json_response(
            {
                "version": 1,
                "public_key": base64.b64encode(
                    state.recipient.public_key().public_bytes_raw()
                ).decode(),
            }
        )
        response.set_cookie("ads_csrf", "test-csrf")
        return response

    async def accept(request):
        assert request.cookies.get("ads_session") == "test-session"
        assert request.cookies.get("ads_csrf") == request.headers.get("X-CSRF-Token") == "test-csrf"
        body = await request.json()
        assert set(body) == {"envelope"}
        state.envelopes.append(body["envelope"])
        claims = _open(body["envelope"], state.recipient, state.signing_key.public_key())
        assert claims["exp"] - claims["iat"] == 90
        assert set(claims) == {
            "v",
            "iss",
            "aud",
            "purpose",
            "sub",
            "iat",
            "exp",
            "jti",
            "revision",
            "config",
        }
        assert claims["aud"] == "safent-ads-broker"
        assert claims["purpose"] == "composio-config"
        assert len(claims["sub"]) == 64
        state.claims.append(claims)
        return web.json_response({"accepted": True})

    app = web.Application()
    app.router.add_post("/api/v1/auth/exchange", exchange)
    app.router.add_get("/api/v1/internal/composio/channel", get_channel)
    app.router.add_post("/api/v1/internal/composio/lease", accept)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state.endpoint = SimpleNamespace(
        host="ads.safent.internal",
        ip="127.0.0.1",
        port=port,
        ca_path=str(ca),
        ca_fingerprint="test-ca",
    )
    monkeypatch.setattr(
        mod, "get_companion", lambda slug: state.endpoint if slug == "safent-ads" else None
    )
    state.db_path = tmp_path / "state.db"
    state.repo = SQLiteIntegrationsRepository(db_path=state.db_path, vault=secrets.SecretsVault())
    state.repo.set_credential(
        kind="composio", api_key="fixture-composio-key", entity_id="owner-fixture"
    )
    state.repo.set_auth_config(toolkit_slug="googleads", auth_config_id="ac_fixture_google")
    state.repo.set_auth_config(toolkit_slug="metaads", auth_config_id="ac_fixture_meta")
    state.authority = CompanionSsoAuthority(private_key_path=sso_path)
    state.publisher = mod.CompanionComposioPublisher(
        db_path=state.db_path, authority=state.authority
    )
    state.sso_path = sso_path
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_pinned_tls_encrypted_delivery_session_reuse_and_durable_revision(
    channel, caplog, monkeypatch
):
    # An environment proxy cannot receive the assertion or lease.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    assert await channel.publisher.publish() == {"accepted": True}
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.exchange_count == 1
    assert [p["revision"] for p in channel.claims] == [1, 2]
    first = channel.claims[0]
    assert first["config"] == {
        "enabled": True,
        "api_key": "fixture-composio-key",
        "entity_id": "owner-fixture",
        "auth_config_ids": {"googleads": "ac_fixture_google", "metaads": "ac_fixture_meta"},
    }
    assert channel.claims[0]["jti"] != channel.claims[1]["jti"]
    assert "fixture-composio-key" not in repr(channel.envelopes) + caplog.text
    restarted = mod.CompanionComposioPublisher(db_path=channel.db_path, authority=channel.authority)
    assert await restarted.publish() == {"accepted": True}
    assert channel.claims[-1]["revision"] == 3
    with sqlite3.connect(channel.db_path) as conn:
        assert conn.execute("SELECT * FROM companion_composio_revision").fetchall() == [(1, 3)]


@pytest.mark.asyncio
async def test_credentials_auth_config_and_recipient_rotation_are_read_fresh(channel):
    assert await channel.publisher.publish() == {"accepted": True}
    channel.recipient = X25519PrivateKey.generate()
    channel.pin_path.write_text(
        base64.b64encode(channel.recipient.public_key().public_bytes_raw()).decode() + "\n"
    )
    channel.repo.set_credential(
        kind="composio", api_key="rotated-fixture", entity_id="rotated-owner"
    )
    channel.repo.set_auth_config(toolkit_slug="googleads", auth_config_id="ac_rotated")
    channel.repo.clear_auth_config(toolkit_slug="metaads")
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.claims[-1]["config"] == {
        "enabled": True,
        "api_key": "rotated-fixture",
        "entity_id": "rotated-owner",
        "auth_config_ids": {"googleads": "ac_rotated"},
    }
    channel.signing_key = Ed25519PrivateKey.generate()
    channel.sso_path.write_text(base64.b64encode(channel.signing_key.private_bytes_raw()).decode())
    assert await channel.publisher.publish() == {"accepted": True}


@pytest.mark.asyncio
async def test_disabled_and_deleted_credentials_publish_tombstones(channel):
    assert await channel.publisher.publish() == {"accepted": True}
    channel.repo.set_credential(kind="composio", api_key="revoked-fixture", enabled=False)
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.claims[-1]["config"]["enabled"] is False
    assert channel.claims[-1]["config"]["api_key"] == ""
    with sqlite3.connect(channel.db_path) as conn:
        conn.execute("DELETE FROM integrations WHERE kind='composio'")
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.claims[-1]["config"] == {
        "enabled": False,
        "api_key": "",
        "entity_id": "default",
        "auth_config_ids": {},
    }


@pytest.mark.asyncio
async def test_session_rejection_refreshes_once_and_redirect_is_not_followed(channel):
    assert await channel.publisher.publish() == {"accepted": True}
    channel.reject_session_once = True
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.exchange_count == 2
    channel.redirect = True
    assert await channel.publisher.publish() == {"accepted": False}
    assert len(channel.claims) == 2


@pytest.mark.asyncio
async def test_managed_and_ambiguous_policy_deny_before_network_or_vault(channel, monkeypatch):
    monkeypatch.setattr(mod, "read_ads_policy", lambda _: SimpleNamespace(mode="managed"))
    assert await channel.publisher.publish() == {"accepted": False}
    assert channel.exchange_count == 0
    assert channel.claims == []

    def unavailable(_):
        raise PermissionError("fixture-sensitive-error")

    monkeypatch.setattr(mod, "read_ads_policy", unavailable)
    assert await channel.publisher.publish() == {"accepted": False}
    assert channel.exchange_count == 0


@pytest.mark.asyncio
async def test_policy_rechecked_after_channel_before_vault_reveal(channel, monkeypatch):
    monkeypatch.setattr(
        mod,
        "read_ads_policy",
        lambda _: SimpleNamespace(mode="managed") if channel.channel_count else None,
    )
    assert await channel.publisher.publish() == {"accepted": False}
    assert channel.channel_count == 1
    assert channel.claims == []


@pytest.mark.asyncio
async def test_absent_companion_is_optional_and_tls_pin_failure_is_sanitized(channel, monkeypatch):
    monkeypatch.setattr(mod, "get_companion", lambda _: None)
    assert await channel.publisher.publish() == {"accepted": False}
    monkeypatch.setattr(mod, "get_companion", lambda _: channel.endpoint)
    channel.endpoint.ca_path = str(channel.db_path)
    assert await channel.publisher.publish() == {"accepted": False}
    assert channel.exchange_count == 0


@pytest.mark.asyncio
async def test_missing_google_preparation_is_bounded_and_failure_still_publishes(
    channel, monkeypatch
):
    from hermes.shell_server.integrations import ads_setup

    channel.repo.clear_auth_config(toolkit_slug="googleads")
    prepare = AsyncMock(side_effect=RuntimeError("fixture-sensitive-error"))
    monkeypatch.setattr(ads_setup, "prepare_composio_ads_configs", prepare)
    assert await channel.publisher.publish() == {"accepted": True}
    assert await channel.publisher.publish() == {"accepted": True}
    assert prepare.await_count == 1
    assert channel.claims[-1]["config"]["auth_config_ids"] == {"metaads": "ac_fixture_meta"}
    channel.repo.set_credential(kind="composio", api_key="disabled-fixture", enabled=False)
    channel.publisher._prepare_after = 0
    assert await channel.publisher.publish() == {"accepted": True}
    assert prepare.await_count == 1
    assert channel.claims[-1]["config"]["enabled"] is False


@pytest.mark.asyncio
async def test_companion_identity_change_reauthenticates(channel):
    assert await channel.publisher.publish() == {"accepted": True}
    channel.endpoint.ca_fingerprint = "rotated-test-ca"
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.exchange_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["wrong", "missing", "unsafe", "malformed", "empty", "short", "low_order"]
)
async def test_untrusted_recipient_pin_never_reads_vault_or_posts_lease(
    channel, monkeypatch, failure
):
    decrypt = Mock(side_effect=AssertionError("must not decrypt"))
    monkeypatch.setattr(secrets.SecretsVault, "decrypt", decrypt)
    if failure == "wrong":
        channel.pin_path.write_text(
            base64.b64encode(X25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
        )
    elif failure == "missing":
        channel.pin_path.unlink()
    elif failure == "unsafe":
        monkeypatch.setattr(companions, "is_companion_secret_file_trustworthy", lambda _: False)
    else:
        values = {
            "malformed": "not a public key!",
            "empty": "",
            "short": "YQ==",
            "low_order": base64.b64encode(bytes(32)).decode(),
        }
        channel.pin_path.write_text(values[failure])
    assert await channel.publisher.publish() == {"accepted": False}
    decrypt.assert_not_called()
    assert channel.envelopes == []


@pytest.mark.asyncio
async def test_pin_rotation_reauthenticates_and_substitution_after_valid_lease_denies(
    channel, monkeypatch
):
    assert await channel.publisher.publish() == {"accepted": True}
    channel.recipient = X25519PrivateKey.generate()
    decrypt = Mock(side_effect=AssertionError("must not decrypt"))
    original_decrypt = secrets.SecretsVault.decrypt
    monkeypatch.setattr(secrets.SecretsVault, "decrypt", decrypt)
    assert await channel.publisher.publish() == {"accepted": False}
    decrypt.assert_not_called()
    assert len(channel.envelopes) == 1
    monkeypatch.setattr(secrets.SecretsVault, "decrypt", original_decrypt)
    channel.pin_path.write_text(
        base64.b64encode(channel.recipient.public_key().public_bytes_raw()).decode() + "\n"
    )
    assert await channel.publisher.publish() == {"accepted": True}
    assert channel.exchange_count == 2


def test_recipient_pin_location_is_fixed():
    assert Path("/etc/hermes/companions/ads-composio-channel.pub") == mod._RECIPIENT_PIN_PATH


@pytest.mark.asyncio
@pytest.mark.parametrize("uid,proxy_uid", [(1000, 880), (0, 880), (9999, 880), (880, None)])
async def test_fixed_purpose_dbus_denies_unauthorized_before_publisher(uid, proxy_uid):
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=SimpleNamespace(),
        authorized_uids=frozenset({1000}),
        proxy_uid=proxy_uid,
    )
    publisher = SimpleNamespace(publish=AsyncMock(return_value={"accepted": True}))
    wiring._companion_composio_publisher_instance = publisher
    with pytest.raises(DbusAuthorizationError):
        await wiring.publish_companion_composio_lease(sender_uid=uid)
    publisher.publish.assert_not_called()


@pytest.mark.asyncio
async def test_fixed_purpose_dbus_returns_acceptance_only():
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=SimpleNamespace(),
        authorized_uids=frozenset({1000}),
        proxy_uid=880,
    )
    wiring._companion_composio_publisher_instance = SimpleNamespace(
        publish=AsyncMock(return_value={"accepted": True})
    )
    assert await wiring.publish_companion_composio_lease(sender_uid=880) == {"accepted": True}


@pytest.mark.parametrize(
    "path",
    [
        "api/v1/internal/composio/channel",
        "api/v1/internal/composio/lease",
        "api/v1/internal",
        "/api/v1/internal/composio/lease",
        "api/v1/foo/../internal/composio/lease",
        "api/v1/%69nternal/composio/lease",
        "api/v1/%2569nternal/composio/lease",
        "api/v1/internal%2fcomposio/lease",
    ],
)
def test_internal_channel_not_accessible_to_browser_bridge(path):
    for method in ("GET", "POST", "PUT", "DELETE"):
        assert not _classify_path(path, method=method).allowed


def test_dbus_export_and_policy_only_grant_service_principal():
    import xml.etree.ElementTree as ET

    from dbus_fast.service import ServiceInterface

    from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import Runtime1ServiceInterface

    interface = Runtime1ServiceInterface(wiring=SimpleNamespace())
    methods = [
        m
        for m in ServiceInterface._get_methods(interface)
        if m.name == "PublishCompanionComposioLease"
    ]
    assert len(methods) == 1
    assert methods[0].in_signature == ""
    policy = ET.parse(
        Path(__file__).resolve().parents[3] / "ops/agents-os-edition/dbus/org.hermes.Runtime1.conf"
    )
    grants = [
        p.attrib.get("user")
        for p in policy.findall("policy")
        for a in p.findall("allow")
        if a.attrib.get("send_member") == "PublishCompanionComposioLease"
    ]
    assert grants == ["hermes"]
