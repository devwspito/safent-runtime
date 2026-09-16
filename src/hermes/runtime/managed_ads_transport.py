"""One request, one exact grant, one ephemeral token. Never retries a write."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from http import HTTPStatus
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp

from hermes.config_sync.ads_policy_contract import AdsBindingSpec
from hermes.config_sync.signature import verify_bundle
from hermes.instance.association_store import SQLiteAssociationStore
from hermes.runtime.managed_ads_policy import ManagedAdsUnavailable, read_ads_policy
from hermes.security.configuration_lock import configuration_lock

TOOLS = frozenset(
    {
        "list_platform_accounts",
        "list_campaigns",
        "get_entity_metrics",
        "propose_budget_change",
        "propose_pause",
        "propose_ad_child",
        "get_proposal",
        "get_approval_review",
    }
)
MAX_RESPONSE_BYTES = 524288
MAX_TOKEN_BYTES = 8192
MAX_ARGUMENT_BYTES = 32768
TOKEN_TTL_SECONDS = 120


class JsonPost(Protocol):
    async def __call__(
        self, url: str, bearer: str, body: dict[str, Any], *, forbidden: tuple[str, ...]
    ) -> dict[str, Any]: ...


async def post_json(
    url: str, bearer: str, body: dict[str, Any], *, forbidden: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Fixed server-selected HTTPS paths; no proxy, redirects, cookies or retry."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
            raise ValueError
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError
        async with (
            asyncio.timeout(8),
            aiohttp.ClientSession(
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=7, connect=3),
            ) as session,
            session.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {bearer}"},
                allow_redirects=False,
            ) as response,
        ):
            if response.status != HTTPStatus.OK:
                raise ValueError
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError
            if any(value and value.encode() in raw for value in forbidden):
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
    except (ValueError, TypeError, aiohttp.ClientError, TimeoutError):
        raise ManagedAdsUnavailable() from None


def _checked_token(body: dict[str, Any], binding: AdsBindingSpec, public_key: str) -> str:
    try:
        token = body["grant_token"]
        if not isinstance(token, str) or not 1 <= len(token) <= MAX_TOKEN_BYTES:
            raise ValueError
        encoded, signature = token.split(".")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        claims = json.loads(raw)
        expected = binding.model_dump()
        if set(claims) != set(expected) | {"v", "purpose", "aud", "iat", "exp", "jti"}:
            raise ValueError
        if json.dumps({key: claims[key] for key in expected}, sort_keys=True) != json.dumps(
            expected, sort_keys=True
        ):
            raise ValueError
        canonical = json.dumps(
            claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        if (
            raw != canonical
            or claims["v"] != 1
            or type(claims["v"]) is not int
            or claims["purpose"] != "ads_account_delegation"
            or claims["aud"] != "safent-ads-central"
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or claims["exp"] - claims["iat"] != TOKEN_TTL_SECONDS
            or not claims["iat"] <= time.time() < claims["exp"]
            or not verify_bundle(
                payload_canonical=raw, signature_hex=signature, pubkey_hex=public_key
            )
        ):
            raise ValueError
        return token
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ManagedAdsUnavailable() from None


class ManagedAdsTransport:
    def __init__(self, store: SQLiteAssociationStore, *, post: JsonPost = post_json) -> None:
        self.store, self.post = store, post

    async def call(
        self,
        grant_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or name not in TOOLS or not isinstance(arguments, dict):
            raise ManagedAdsUnavailable()
        try:
            encoded = json.dumps(arguments, allow_nan=False)
            if len(encoded.encode()) > MAX_ARGUMENT_BYTES:
                raise ValueError
            frozen = json.loads(encoded)
            expected = (
                AdsBindingSpec.model_validate(expected_binding)
                if expected_binding is not None
                else None
            )
        except (ValueError, TypeError):
            raise ManagedAdsUnavailable() from None
        with configuration_lock(self.store.db_path):
            policy = read_ads_policy(self.store.db_path)
            if policy is None or policy.mode != "managed" or policy.central_origin is None:
                raise ManagedAdsUnavailable()
            binding = next((item for item in policy.bindings if item.grant_id == grant_id), None)
            association = self.store.get()
            if binding is None or association is None:
                raise ManagedAdsUnavailable()
            # Caller snapshot is only a precondition, never delegated authority.
            # A fresh signed binding remains the sole principal for bootstrap.
            if expected is not None and expected != binding:
                raise ManagedAdsUnavailable()
            secret = self.store.reveal_instance_secret()
            if not secret:
                raise ManagedAdsUnavailable()
        # No config/SQLite lock held during HTTP. There is deliberately NO token cache.
        issued = await self.post(
            association.cloud_endpoint.rstrip("/") + "/v1/ads/grants/token",
            secret,
            {"grant_id": grant_id},
            forbidden=(secret,),
        )
        token = _checked_token(issued, binding, association.signing_pubkey_hex)
        with configuration_lock(self.store.db_path):
            if self.store.get() != association or read_ads_policy(self.store.db_path) != policy:
                raise ManagedAdsUnavailable()
        result = await self.post(
            policy.central_origin + "/api/v1/managed/tools/" + name,
            token,
            frozen,
            forbidden=(secret, token),
        )
        # A concurrent local revoke/reassociation also denies disclosure of the result.
        with configuration_lock(self.store.db_path):
            if self.store.get() != association or read_ads_policy(self.store.db_path) != policy:
                raise ManagedAdsUnavailable()
        return result
