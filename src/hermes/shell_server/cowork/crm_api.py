"""Owner-authenticated Community projection of Enterprise's read-only CRM API.

No CRM credentials, arbitrary URLs, writes or new agent executor. The existing
shell bearer middleware protects both routes; Enterprise checks the live grant
on every request. A pairing snapshot prevents stale UI intent from following a
new organisation and discards results after a local authority change.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes.instance.association_store import SQLiteAssociationStore
from hermes.instance.infrastructure.http_control_plane_client import _validate_cloud_endpoint
from hermes.security.configuration_lock import configuration_lock

_MAX_RESPONSE = 1_100_000
_MAX_PATH = 2048
_MAX_DEPTH = 32
_MAX_CONNECTIONS = 100
_MAX_NAME = 200
_MAX_OPERATIONS = 20
_MAX_REQUEST = 8192
_CONTEXT_LENGTH = 64
_HTTP_OK = 200


def _fail(code, status=503):
    raise HTTPException(status, detail={"code": code}, headers={"Cache-Control": "no-store"})


def _path(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_PATH
        and value.startswith("/")
        and not value.startswith("//")
        and not any(char in value for char in "?#%\\{}")
        and not any(char.isspace() or ord(char) < ord("!") for char in value)
        and not any(part in (".", "..") for part in value.split("/"))
    )


def _strict_json(raw):
    def invalid(_value):
        raise ValueError("Non-finite JSON")

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    value = json.loads(raw, parse_constant=invalid, object_pairs_hook=unique)
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > _MAX_DEPTH:
            raise ValueError("Deep JSON")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON")
        children = (
            item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        )
        stack.extend((child, depth + 1) for child in children)
    return value


class EnterpriseCrm:
    def __init__(self, store, context_key, transport=None):
        self.store, self.key, self.transport = store, context_key, transport

    def snapshot(self):
        try:
            with configuration_lock(self.store.db_path):
                association = self.store.get()
                if association is None or association.state != "active":
                    _fail("crm_not_associated", 409)
                secret = self.store.reveal_instance_secret()
                if not secret:
                    _fail("crm_not_associated", 409)
                base = association.cloud_endpoint.rstrip("/")
                parsed = urlsplit(base)
                _validate_cloud_endpoint(base)
                if (
                    parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.port not in (None, 443)
                ):
                    raise ValueError("Invalid paired endpoint")
                identity = json.dumps(
                    [
                        association.instance_id,
                        association.tenant_id,
                        association.paired_at,
                        base,
                        association.signing_pubkey_hex,
                        secret,
                    ],
                    separators=(",", ":"),
                ).encode()
                context = hmac.new(self.key, identity, hashlib.sha256).hexdigest()
                return context, base, secret
        except HTTPException:
            raise
        except Exception:
            _fail("crm_unavailable")

    async def call(self, path="", *, context=None, operation=None):  # noqa: PLR0912 - explicit boundary checks
        snapshot, base, secret = self.snapshot()
        if context is not None and not hmac.compare_digest(snapshot, context):
            _fail("crm_context_changed", 409)
        try:
            async with (
                asyncio.timeout(20),
                httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=18,
                    headers={"Authorization": f"Bearer {secret}", "Accept-Encoding": "identity"},
                ) as client,
                client.stream(
                    "POST" if operation is not None else "GET",
                    base + "/v1/crm" + path,
                    **({"json": {"path": operation}} if operation is not None else {}),
                ) as reply,
            ):
                if reply.status_code != _HTTP_OK:
                    status = reply.status_code
                    _fail(
                        {
                            403: "crm_forbidden",
                            404: "crm_not_found",
                            409: "crm_conflict",
                            429: "crm_rate_limited",
                        }.get(status, "crm_unavailable"),
                        status if status in (403, 404, 409, 429) else 503,
                    )
                if reply.headers.get("content-encoding", "identity") != "identity":
                    _fail("crm_response_invalid", 502)
                raw = bytearray()
                async for chunk in reply.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE:
                        _fail("crm_response_invalid", 502)
                value = _strict_json(raw)
            current, _, _ = self.snapshot()
            if not hmac.compare_digest(snapshot, current):
                _fail("crm_context_changed", 409)
            if operation is None:
                result = self.project_connections(value)
            else:
                if (
                    not isinstance(value, dict)
                    or value.get("untrusted_external_data") is not True
                    or value.get("operation") != {"method": "GET", "path": operation}
                    or "data" not in value
                ):
                    _fail("crm_response_invalid", 502)
                result = {
                    "data": value["data"],
                    "operation": value["operation"],
                    "untrusted_external_data": True,
                }
            # Defense in depth, not general DLP. Never return a literal echo
            # of the instance credential even if the remote service misbehaves.
            if secret in json.dumps(result, ensure_ascii=False):
                _fail("crm_response_invalid", 502)
            return {**result, "context": snapshot}
        except HTTPException:
            raise
        except (ValueError, TypeError, RecursionError):
            _fail("crm_response_invalid", 502)
        except (httpx.HTTPError, TimeoutError):
            _fail("crm_unavailable")

    @staticmethod
    def project_connections(value):
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("connections"), list)
            or len(value["connections"]) > _MAX_CONNECTIONS
        ):
            _fail("crm_response_invalid", 502)
        connections, seen = [], set()
        for item in value["connections"]:
            if not isinstance(item, dict):
                _fail("crm_response_invalid", 502)
            if not isinstance(item.get("id"), str):
                _fail("crm_response_invalid", 502)
            identifier = str(UUID(item["id"]))
            name, operations = item.get("name"), item.get("operations")
            if (
                identifier in seen
                or not isinstance(name, str)
                or not 0 < len(name) <= _MAX_NAME
                or item.get("read_only") is not True
                or not isinstance(operations, list)
                or not 0 < len(operations) <= _MAX_OPERATIONS
            ):
                _fail("crm_response_invalid", 502)
            if any(
                not isinstance(op, dict) or op.get("method") != "GET" or not _path(op.get("path"))
                for op in operations
            ):
                _fail("crm_response_invalid", 502)
            seen.add(identifier)
            connections.append(
                {
                    "id": identifier,
                    "name": name,
                    "operations": [{"method": "GET", "path": op["path"]} for op in operations],
                    "read_only": True,
                }
            )
        return {"connections": connections, "limit": _MAX_CONNECTIONS}


def create_crm_router(db_path=None, vault=None, *, store=None, context_key=None, transport=None):
    service = EnterpriseCrm(
        store if store is not None else SQLiteAssociationStore(db_path=db_path, vault=vault),
        context_key
        if context_key is not None
        else vault.derive_subkey(label="crm-pairing-context"),
        transport,
    )
    router = APIRouter(prefix="/api/v1/crm", tags=["crm"])

    @router.get("")
    async def list_connections():
        return JSONResponse(await service.call(), headers={"Cache-Control": "no-store"})

    @router.post("/{connection}/read")
    async def read(connection: UUID, request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            _fail("crm_request_invalid", 415)
        try:
            async with asyncio.timeout(5):
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > _MAX_REQUEST:
                        _fail("crm_request_invalid", 413)
                body = _strict_json(raw)
            if (
                not isinstance(body, dict)
                or set(body) != {"context", "path"}
                or not _path(body["path"])
                or not isinstance(body["context"], str)
                or len(body["context"]) != _CONTEXT_LENGTH
                or any(c not in "0123456789abcdef" for c in body["context"])
            ):
                _fail("crm_request_invalid", 422)
        except (ValueError, TypeError, RecursionError, TimeoutError):
            _fail("crm_request_invalid", 422)
        return JSONResponse(
            await service.call(
                f"/{connection}/read", context=body["context"], operation=body["path"]
            ),
            headers={"Cache-Control": "no-store"},
        )

    return router
