"""Daemon-only publication of short-lived Composio configuration to local Ads.

The recipient comes exclusively from the current, root-staged companion
registry and its pinned TLS channel. No caller may supply a recipient key,
destination, claims, or credential. Only an acceptance boolean leaves D-Bus.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import ssl
import time
import uuid
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hermes.agents_os.infrastructure.companion_sso_authority import (
    CompanionSsoAuthority,
    _b64url_encode,
    _derive_subject,
    _load_sso_private_key,
)
from hermes.runtime.managed_ads_policy import read_ads_policy
from hermes.shell_server.companion_net import FixedIpResolver
from hermes.shell_server.companions import get_companion
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security.secrets import SecretsVault

_SLUG = "safent-ads"
_BASE_PATH = "/api/v1/internal/composio"
_TTL = 90
_MAX_BODY = 8192
_HKDF_INFO = b"safent-composio-lease-v1"
_AAD = b"safent-ads-broker:composio-config:v1"
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_X25519_KEY_BYTES = 32
_VAULT_AAD = "integration:composio"  # Authenticated data, not a password.


class _PublicationUnavailable(RuntimeError):
    """Constant reason only: never expose upstream bodies or secret values."""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _seal(*, claims: dict, private_key, recipient: bytes) -> str:
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared)
    payload = _canonical(claims)
    token = f"{_b64url_encode(payload)}.{_b64url_encode(private_key.sign(payload))}"
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, token.encode(), _AAD)
    return _b64url_encode(
        _canonical(
            {
                "v": 1,
                "ephemeral_public_key": _b64url_encode(ephemeral.public_key().public_bytes_raw()),
                "nonce": _b64url_encode(nonce),
                "ciphertext": _b64url_encode(ciphertext),
            }
        )
    )


def _next_revision(db_path: Path) -> int:
    # Only a monotonic counter is persisted here, never plaintext or its hash.
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS companion_composio_revision "
            "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO companion_composio_revision VALUES (1, 0)")
        conn.execute("UPDATE companion_composio_revision SET revision=revision+1 WHERE id=1")
        revision = conn.execute(
            "SELECT revision FROM companion_composio_revision WHERE id=1"
        ).fetchone()[0]
        conn.execute("COMMIT")
    return revision


class CompanionComposioPublisher:
    """Single daemon instance; serialized leases and private SSO session cache."""

    def __init__(self, *, db_path: Path, authority: CompanionSsoAuthority) -> None:
        self._db_path = db_path
        self._authority = authority
        self._lock = asyncio.Lock()
        self._session_cookie: str | None = None
        self._session_expires = 0.0
        self._endpoint_identity = None
        self._prepare_after = 0.0

    def _local_allowed(self) -> bool:
        policy = read_ads_policy(self._db_path)
        return policy is None or policy.mode == "free"

    def _clear_session(self) -> None:
        self._session_cookie = None
        self._session_expires = 0.0

    async def publish(self) -> dict[str, bool]:
        """Best effort, with bounded IO and sanitized outcome. No secret return."""
        async with self._lock:
            try:
                if not self._local_allowed():
                    self._clear_session()
                    return {"accepted": False}
                endpoint = get_companion(_SLUG)
                if endpoint is None:
                    self._clear_session()
                    return {"accepted": False}
                identity = (endpoint.host, endpoint.ip, endpoint.port, endpoint.ca_fingerprint)
                if identity != self._endpoint_identity:
                    self._clear_session()
                    self._endpoint_identity = identity
                connector = aiohttp.TCPConnector(
                    resolver=FixedIpResolver(hostname=endpoint.host, ip=endpoint.ip),
                    ssl=ssl.create_default_context(cafile=endpoint.ca_path),
                )
                async with aiohttp.ClientSession(
                    connector=connector,
                    trust_env=False,
                    auto_decompress=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=aiohttp.ClientTimeout(total=8, connect=3),
                ) as session:
                    base = f"https://{endpoint.host}:{endpoint.port}"
                    return await self._publish_to_channel(session, base)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never relay secret-bearing exceptions
                self._clear_session()
                return {"accepted": False}

    async def _session(self, session, base: str) -> str:
        if self._session_cookie and self._session_expires > time.monotonic():
            return self._session_cookie
        if not self._local_allowed():
            raise _PublicationUnavailable()
        assertion = self._authority.mint_owner_assertion(slug=_SLUG)
        async with session.post(
            base + "/api/v1/auth/exchange",
            json={"assertion": assertion.assertion},
            allow_redirects=False,
        ) as response:
            if response.status != _HTTP_OK or "ads_session" not in response.cookies:
                raise _PublicationUnavailable()
            morsel = response.cookies["ads_session"]
            self._session_cookie = morsel.value
            raw_age = morsel.get("max-age", "")
            ttl = min(float(raw_age), 3600) if str(raw_age).isdigit() else 3600
            self._session_expires = time.monotonic() + max(0, ttl - 5)
        return self._session_cookie

    async def _channel(self, session, base: str) -> tuple[bytes, str, str]:
        # A stale owner session gets exactly one fresh assertion/exchange.
        for attempt in range(2):
            cookie = await self._session(session, base)
            async with session.get(
                base + _BASE_PATH + "/channel",
                headers={"Cookie": f"ads_session={cookie}"},
                allow_redirects=False,
            ) as response:
                if response.status == _HTTP_UNAUTHORIZED and attempt == 0:
                    self._clear_session()
                    continue
                if response.status != _HTTP_OK or "ads_csrf" not in response.cookies:
                    raise _PublicationUnavailable()
                body = await _read_json(response)
                if (
                    not isinstance(body, dict)
                    or type(body.get("version")) is not int
                    or body["version"] != 1
                ):
                    raise _PublicationUnavailable()
                recipient = base64.b64decode(body["public_key"], validate=True)
                if len(recipient) != _X25519_KEY_BYTES:
                    raise _PublicationUnavailable()
                return recipient, cookie, response.cookies["ads_csrf"].value
        raise _PublicationUnavailable()

    async def _prepare(self, repo: SQLiteIntegrationsRepository) -> None:
        integration = repo.get_or_none(kind="composio")
        if (
            integration is None
            or not integration.enabled
            or not integration.has_api_key
            or "googleads" in repo.auth_config_ids()
            or time.monotonic() < self._prepare_after
        ):
            return
        self._prepare_after = time.monotonic() + 300
        try:
            from hermes.shell_server.integrations.ads_setup import (  # noqa: PLC0415
                prepare_composio_ads_configs,
            )

            async with asyncio.timeout(8):
                await prepare_composio_ads_configs(self._db_path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - publish actual config even if prepare fails
            return

    def _configuration(self, vault: SecretsVault) -> dict:
        # Read one SQLite snapshot: a concurrent key rotation must never mix
        # the old entity/config selections with the newly written credential.
        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT enabled, entity_id, api_key_ciphertext FROM integrations "
                "WHERE kind='composio'"
            ).fetchone()
            configs = dict(
                conn.execute(
                    "SELECT toolkit_slug, auth_config_id FROM integration_auth_configs "
                    "WHERE kind='composio' AND toolkit_slug IN ('googleads', 'metaads')"
                ).fetchall()
            )
            conn.execute("COMMIT")
        enabled = bool(row and row[0] and row[2])
        api_key = vault.decrypt(secret_id=_VAULT_AAD, blob=bytes(row[2])) if enabled else ""
        enabled = enabled and bool(api_key)
        return {
            "enabled": enabled,
            "api_key": api_key or "",
            "entity_id": row[1] if row else "default",
            "auth_config_ids": configs if enabled else {},
        }

    async def _publish_to_channel(self, session, base: str) -> dict[str, bool]:
        recipient, cookie, csrf = await self._channel(session, base)
        if not self._local_allowed():
            raise _PublicationUnavailable()
        vault = SecretsVault()
        repo = SQLiteIntegrationsRepository(db_path=self._db_path, vault=vault)
        await self._prepare(repo)
        # Recheck after every external await before revealing the vault.
        if not self._local_allowed():
            raise _PublicationUnavailable()
        config = self._configuration(vault)
        now = int(time.time())
        claims = {
            "v": 1,
            "iss": "safent-runtime",
            "aud": "safent-ads-broker",
            "purpose": "composio-config",
            "sub": _derive_subject(),
            "iat": now,
            "exp": now + _TTL,
            "jti": str(uuid.uuid4()),
            "revision": _next_revision(self._db_path),
            "config": config,
        }
        envelope = _seal(
            claims=claims,
            private_key=_load_sso_private_key(path=self._authority._private_key_path),
            recipient=recipient,
        )
        if not self._local_allowed():
            raise _PublicationUnavailable()
        async with session.post(
            base + _BASE_PATH + "/lease",
            json={"envelope": envelope},
            headers={"Cookie": f"ads_session={cookie}; ads_csrf={csrf}", "X-CSRF-Token": csrf},
            allow_redirects=False,
        ) as response:
            if response.status == _HTTP_UNAUTHORIZED:
                self._clear_session()
            if response.status != _HTTP_OK:
                return {"accepted": False}
            result = await _read_json(response)
            return {"accepted": isinstance(result, dict) and result.get("accepted") is True}


async def _read_json(response) -> dict:
    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
        raise _PublicationUnavailable()
    body = bytearray()
    async for chunk in response.content.iter_chunked(2048):
        body.extend(chunk)
        if len(body) > _MAX_BODY:
            raise _PublicationUnavailable()
    return json.loads(body)
