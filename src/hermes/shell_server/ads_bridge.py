"""ads_bridge — Safent -> safent-ads session bridge (026, contracts/sso.md).

Two routes:
  POST/DELETE /api/v1/ads/bridge/session — mint/clear the `ads_bridge`
    cookie. Gated by the EXISTING webui bearer middleware (main.py) since
    the path starts with /api/v1/ — nothing new to enforce here.
  /ads/{path:path} — same-origin reverse proxy to the companion. Gated by
    the `ads_bridge` cookie itself (default-deny, GET included — this route
    lives OUTSIDE /api/v1/* on purpose: the browser loads it as a plain
    same-origin resource, an iframe navigation cannot carry a bearer).

Trust boundary (sso.md §3): the daemon is the ONLY signer of owner
assertions and the ONLY reader of the SSO private key (T004,
`org.hermes.Runtime1.MintCompanionOwnerAssertion`). This module never reads
that key — it asks the daemon over D-Bus (`app.state.dbus_proxy`) for an
opaque assertion string and exchanges it with the companion.

`AdsSessionJar` holds the companion's `ads_session` cookie IN PROCESS — it
NEVER reaches the browser (I-2). Lazy exchange fills the jar on first use
and on upstream 401, with at most ONE retry (sso.md §2). Confirmed mutations
are never replayed under a replacement session; they return the original 401.
"""

from __future__ import annotations

import hmac
import logging
import ssl
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from hermes.shell_server.companion_net import FixedIpResolver
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.ads_bridge")

_COMPANION_SLUG = "safent-ads"

_BRIDGE_COOKIE_NAME = "ads_bridge"
_BRIDGE_COOKIE_MAX_AGE_S = 2592000  # 30 days
_SESSION_COOKIE_NAME = "ads_session"
_CSRF_COOKIE_NAME = "ads_csrf"

_FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "accept", "accept-language", "content-type", "content-length",
        "if-none-match", "x-csrf-token", "x-action-confirmation",
    }
)
_HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection", "keep-alive", "transfer-encoding", "content-encoding",
        "content-length", "set-cookie",
    }
)
_FORWARDED_PREFIX = "/ads"
_MAX_BODY_BYTES = 8 * 1024 * 1024
_CONNECT_TIMEOUT_S = 10.0
_TOTAL_TIMEOUT_S = 30.0
_EXCHANGE_TIMEOUT_S = 10.0
_DENIED_MCP_PREFIX = "mcp"
_DENIED_LOGIN_PATHS = frozenset({"api/v1/auth/login", "api/v1/auth/totp"})
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_EXACT_PATHS = frozenset({"", "favicon.ico"})
_ALLOWED_PATH_PREFIXES = ("assets/", "api/v1/")
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401


class AdsBridgeError(RuntimeError):
    """Base class — carries an HTTP status + machine-readable code, never a
    stack trace or upstream secret."""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class AdsSessionUnavailableError(AdsBridgeError):
    def __init__(self, message: str) -> None:
        super().__init__(
            status_code=503, code="ADS_SESSION_UNAVAILABLE", message=message
        )


class CompanionUnreachableError(AdsBridgeError):
    def __init__(self, message: str) -> None:
        super().__init__(status_code=504, code="COMPANION_TIMEOUT", message=message)


# ---------------------------------------------------------------------------
# ads_bridge cookie — stable HKDF subkey of master.key, same pattern as the
# webui's own stable session bearer (main.py). No server-side store: a
# request is authenticated iff its cookie constant-time-matches this
# derived value.
# ---------------------------------------------------------------------------


def _bridge_cookie_value() -> str:
    from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

    return SecretsVault().derive_subkey(label="ads-bridge-cookie").hex()


def _bridge_cookie_is_valid(request: Request) -> bool:
    presented = request.cookies.get(_BRIDGE_COOKIE_NAME, "")
    if not presented:
        return False
    return hmac.compare_digest(presented, _bridge_cookie_value())


# ---------------------------------------------------------------------------
# AdsSessionJar — in-process holder of the companion's ads_session cookie.
# ---------------------------------------------------------------------------


@dataclass
class _JarEntry:
    cookie: str
    expires_at: float  # time.monotonic() deadline


class AdsSessionJar:
    """Single-owner, in-process jar (sso.md §2/§6). `ads_session` NEVER
    reaches the browser — only this jar ever holds it."""

    def __init__(self) -> None:
        self._entry: _JarEntry | None = None

    def get(self) -> str | None:
        if self._entry is None:
            return None
        if self._entry.expires_at <= time.monotonic():
            self._entry = None
            return None
        return self._entry.cookie

    def set(self, *, cookie: str, ttl_seconds: float) -> None:
        self._entry = _JarEntry(cookie=cookie, expires_at=time.monotonic() + ttl_seconds)

    def clear(self) -> None:
        self._entry = None


# ---------------------------------------------------------------------------
# Path/method allow-list (sso.md §2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PathDecision:
    allowed: bool
    denial_code: str | None = None


def _classify_path(path: str) -> _PathDecision:
    normalized = path.lstrip("/")
    if normalized == _DENIED_MCP_PREFIX or normalized.startswith(f"{_DENIED_MCP_PREFIX}/"):
        return _PathDecision(allowed=False, denial_code="MCP_NOT_BRIDGED")
    if normalized in _DENIED_LOGIN_PATHS:
        return _PathDecision(allowed=False, denial_code="LOGIN_NOT_BRIDGED")
    if normalized in _ALLOWED_EXACT_PATHS or normalized.startswith(_ALLOWED_PATH_PREFIXES):
        return _PathDecision(allowed=True)
    return _PathDecision(allowed=False, denial_code="PATH_NOT_BRIDGED")


# ---------------------------------------------------------------------------
# Owner-session exchange (mint via D-Bus, POST /api/v1/auth/exchange)
# ---------------------------------------------------------------------------


async def _mint_assertion(dbus_proxy: Any) -> str:
    try:
        minted = await dbus_proxy.call_dict("mint_companion_owner_assertion", _COMPANION_SLUG)
    except (AgentUnavailable, HTTPException) as exc:
        raise AdsSessionUnavailableError(f"could not mint owner assertion: {exc}") from exc
    assertion = minted.get("assertion") if isinstance(minted, dict) else None
    if not assertion:
        raise AdsSessionUnavailableError("daemon returned no assertion")
    return assertion


async def _exchange_for_session(endpoint: Any, assertion: str) -> tuple[str, float]:
    ssl_ctx = _pinned_ssl_context(endpoint.ca_path)
    resolver = FixedIpResolver(hostname=endpoint.host, ip=endpoint.ip)
    connector = aiohttp.TCPConnector(resolver=resolver, ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=_EXCHANGE_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
    url = f"https://{endpoint.host}:{endpoint.port}/api/v1/auth/exchange"
    try:
        async with (
            aiohttp.ClientSession(connector=connector, timeout=timeout) as session,
            session.post(url, json={"assertion": assertion}) as response,
        ):
            if response.status != _HTTP_OK:
                raise AdsSessionUnavailableError(
                    f"companion rejected the exchange (HTTP {response.status})"
                )
            return _extract_session_cookie(response)
    except TimeoutError as exc:
        raise CompanionUnreachableError("timed out exchanging the owner assertion") from exc
    except aiohttp.ClientError as exc:
        raise CompanionUnreachableError(f"companion unreachable: {type(exc).__name__}") from exc


def _extract_session_cookie(response: aiohttp.ClientResponse) -> tuple[str, float]:
    morsel = response.cookies.get(_SESSION_COOKIE_NAME)
    if morsel is None:
        raise AdsSessionUnavailableError("exchange response carried no ads_session cookie")
    max_age = morsel.get("max-age") or ""
    ttl = float(max_age) if str(max_age).isdigit() else 3600.0
    return morsel.value, ttl


async def _ensure_session(*, dbus_proxy: Any, endpoint: Any, jar: AdsSessionJar) -> str:
    cookie = jar.get()
    if cookie is not None:
        return cookie
    assertion = await _mint_assertion(dbus_proxy)
    cookie, ttl = await _exchange_for_session(endpoint, assertion)
    jar.set(cookie=cookie, ttl_seconds=ttl)
    return cookie


def _pinned_ssl_context(ca_path: str) -> ssl.SSLContext:
    try:
        return ssl.create_default_context(cafile=ca_path)
    except (OSError, ssl.SSLError) as exc:
        raise CompanionUnreachableError(f"companion CA unreadable: {exc}") from exc


# ---------------------------------------------------------------------------
# Request/response translation
# ---------------------------------------------------------------------------


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }


def _build_cookie_header(request: Request, *, session_cookie: str) -> str:
    csrf = request.cookies.get(_CSRF_COOKIE_NAME)
    parts = [f"{_SESSION_COOKIE_NAME}={session_cookie}"]
    if csrf:
        parts.append(f"{_CSRF_COOKIE_NAME}={csrf}")
    return "; ".join(parts)


async def _read_bounded_body(request: Request) -> bytes:
    """Reads the body while enforcing the 8 MiB cap DURING the read (not
    after) — a hostile client cannot force unbounded buffering before the
    cap is checked."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise AdsBridgeError(
                status_code=413, code="BODY_TOO_LARGE", message="request body exceeds 8 MiB"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _companion_url(endpoint: Any, path: str, query: str) -> str:
    normalized = path.lstrip("/")
    url = f"https://{endpoint.host}:{endpoint.port}/{normalized}"
    return f"{url}?{query}" if query else url


@dataclass(frozen=True)
class _UpstreamResponse:
    """Everything downstream code needs, captured INSIDE the aiohttp
    `async with` block — the raw `ClientResponse` is unusable for a second
    `.read()`/cookie pass once its connection has been released (aiohttp
    raises `ClientConnectionError` rather than serving the cached body)."""

    status: int
    headers: dict[str, str]
    set_cookie_headers: tuple[str, ...]
    body: bytes


def _rewrite_and_split_cookies(
    response: _UpstreamResponse,
) -> tuple[list[str], str | None, float | None]:
    """Split upstream Set-Cookie headers: `ads_csrf` is rewritten (Path=/ads)
    and forwarded to the browser; `ads_session` is retained for the jar and
    NEVER forwarded (I-2); anything else is dropped."""
    forward_headers: list[str] = []
    new_session_cookie: str | None = None
    new_session_ttl: float | None = None
    raw = SimpleCookie()
    for header_value in response.set_cookie_headers:
        raw.load(header_value)
    for name, morsel in raw.items():
        if name == _CSRF_COOKIE_NAME:
            forward_headers.append(_rewritten_csrf_cookie(morsel))
        elif name == _SESSION_COOKIE_NAME:
            new_session_cookie = morsel.value
            max_age = morsel.get("max-age") or ""
            new_session_ttl = float(max_age) if str(max_age).isdigit() else 3600.0
    return forward_headers, new_session_cookie, new_session_ttl


def _rewritten_csrf_cookie(morsel: Any) -> str:
    attrs = [f"{_CSRF_COOKIE_NAME}={morsel.value}", "Path=/ads"]
    if morsel.get("max-age"):
        attrs.append(f"Max-Age={morsel['max-age']}")
    if morsel.get("samesite"):
        attrs.append(f"SameSite={morsel['samesite']}")
    # Secure is kept unless the edge is a plain-http loopback origin (the
    # companion always issues Secure; a loopback shell-server may not be
    # TLS — same W3C "potentially trustworthy origin" carve-out sso.md §2
    # documents for this exact cookie).
    if morsel.get("secure"):
        attrs.append("Secure")
    return "; ".join(attrs)


def _capture_upstream_response(response: aiohttp.ClientResponse, body: bytes) -> _UpstreamResponse:
    return _UpstreamResponse(
        status=response.status,
        headers={
            name: value
            for name, value in response.headers.items()
            if name.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
        },
        set_cookie_headers=tuple(response.headers.getall("Set-Cookie", [])),
        body=body,
    )


# ---------------------------------------------------------------------------
# Core proxy flow — one attempt, with the caller handling the single retry
# ---------------------------------------------------------------------------


async def _proxy_once(
    *, endpoint: Any, request: Request, path: str, session_cookie: str, body: bytes,
) -> _UpstreamResponse:
    url = _companion_url(endpoint, path, request.url.query)
    headers = _forward_headers(request)
    cookie_header = _build_cookie_header(request, session_cookie=session_cookie)
    ssl_ctx = _pinned_ssl_context(endpoint.ca_path)
    resolver = FixedIpResolver(hostname=endpoint.host, ip=endpoint.ip)
    connector = aiohttp.TCPConnector(resolver=resolver, ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=_TOTAL_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
    headers["Cookie"] = cookie_header
    headers["X-Forwarded-Prefix"] = _FORWARDED_PREFIX
    # The origin the BROWSER used, so the companion can build OAuth redirect
    # URIs the browser can actually reach (127.0.0.1:<port>/ads/...), never its
    # unroutable internal name. Set server-side: a client value is overwritten.
    headers["X-Forwarded-Host"] = request.url.netloc
    headers["X-Forwarded-Proto"] = request.url.scheme
    try:
        async with (
            aiohttp.ClientSession(connector=connector, timeout=timeout) as session,
            session.request(
                request.method, url, headers=headers, data=body or None
            ) as response,
        ):
            response_body = await response.read()
            return _capture_upstream_response(response, response_body)
    except TimeoutError as exc:
        raise CompanionUnreachableError("companion did not respond in time") from exc
    except aiohttp.ClientError as exc:
        raise CompanionUnreachableError(f"companion unreachable: {type(exc).__name__}") from exc


async def _proxy_request(
    *, app_state: Any, request: Request, path: str
) -> Response:
    from hermes.shell_server.companions import get_companion  # noqa: PLC0415

    decision = _classify_path(path)
    if not decision.allowed:
        return _error_response(403, decision.denial_code or "PATH_NOT_BRIDGED")

    if request.method not in _ALLOWED_METHODS:
        return _error_response(405, "METHOD_NOT_BRIDGED")

    endpoint = get_companion(_COMPANION_SLUG)
    if endpoint is None:
        return _error_response(504, "COMPANION_TIMEOUT")

    jar: AdsSessionJar = app_state.ads_session_jar
    body = await _read_bounded_body(request)

    try:
        session_cookie = await _ensure_session(
            dbus_proxy=app_state.dbus_proxy, endpoint=endpoint, jar=jar
        )
        response = await _proxy_once(
            endpoint=endpoint, request=request, path=path,
            session_cookie=session_cookie, body=body,
        )
        if response.status == _HTTP_UNAUTHORIZED:
            jar.clear()
            # A confirmation belongs to the exact original companion session.
            # Never silently exchange identity and replay a confirmed mutation.
            if not request.headers.get("x-action-confirmation"):
                session_cookie = await _ensure_session(
                    dbus_proxy=app_state.dbus_proxy, endpoint=endpoint, jar=jar
                )
                response = await _proxy_once(
                    endpoint=endpoint, request=request, path=path,
                    session_cookie=session_cookie, body=body,
                )
    except AdsBridgeError as exc:
        return _error_response(exc.status_code, exc.code)

    return _translate_response(response, jar=jar)


def _translate_response(response: _UpstreamResponse, *, jar: AdsSessionJar) -> Response:
    cookie_headers, new_session, new_ttl = _rewrite_and_split_cookies(response)
    if new_session is not None and new_ttl is not None:
        jar.set(cookie=new_session, ttl_seconds=new_ttl)
    out = Response(
        content=response.body, status_code=response.status, headers=response.headers
    )
    for cookie_header in cookie_headers:
        out.headers.append("set-cookie", cookie_header)
    return out


def _error_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status_code)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_ads_bridge_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/ads/bridge/session")
    async def mint_bridge_session(request: Request) -> Response:
        """Gated by the EXISTING webui bearer middleware (path is under
        /api/v1/*) — no additional auth here. Issues the stable ads_bridge
        cookie and reports companion readiness (T004 get_companion_health)."""
        status, reason = await _companion_readiness(request.app.state.dbus_proxy)
        response = JSONResponse({"status": status, "reason": reason})
        response.set_cookie(
            _BRIDGE_COOKIE_NAME, _bridge_cookie_value(),
            max_age=_BRIDGE_COOKIE_MAX_AGE_S, httponly=True,
            samesite="strict", path="/ads",
        )
        return response

    @router.delete("/api/v1/ads/bridge/session")
    async def clear_bridge_session(request: Request) -> Response:
        jar: AdsSessionJar = request.app.state.ads_session_jar
        await _logout_upstream(jar)
        jar.clear()
        response = JSONResponse({"status": "cleared"})
        response.delete_cookie(_BRIDGE_COOKIE_NAME, path="/ads")
        return response

    @router.api_route("/ads", methods=sorted(_ALLOWED_METHODS))
    async def proxy_ads_root(request: Request) -> Response:
        if not _bridge_cookie_is_valid(request):
            return _error_response(401, "BRIDGE_COOKIE_REQUIRED")
        return await _proxy_request(app_state=request.app.state, request=request, path="")

    @router.api_route("/ads/{path:path}", methods=sorted(_ALLOWED_METHODS))
    async def proxy_ads_path(request: Request, path: str) -> Response:
        if not _bridge_cookie_is_valid(request):
            return _error_response(401, "BRIDGE_COOKIE_REQUIRED")
        return await _proxy_request(app_state=request.app.state, request=request, path=path)

    return router


async def _companion_readiness(dbus_proxy: Any) -> tuple[str, str | None]:
    try:
        health = await dbus_proxy.call_dict("get_companion_health", _COMPANION_SLUG)
    except (AgentUnavailable, HTTPException):
        return "unavailable", "unreachable"
    state = health.get("state") if isinstance(health, dict) else None
    if state == "ready":
        return "ready", None
    return "unavailable", state or "unreachable"


async def _logout_upstream(jar: AdsSessionJar) -> None:
    """Best-effort upstream logout — never blocks the client-facing DELETE
    on a companion/network failure (the jar is cleared unconditionally by
    the caller either way)."""
    from hermes.shell_server.companions import get_companion  # noqa: PLC0415

    session_cookie = jar.get()
    if session_cookie is None:
        return
    endpoint = get_companion(_COMPANION_SLUG)
    if endpoint is None:
        return
    try:
        ssl_ctx = _pinned_ssl_context(endpoint.ca_path)
        resolver = FixedIpResolver(hostname=endpoint.host, ip=endpoint.ip)
        connector = aiohttp.TCPConnector(resolver=resolver, ssl=ssl_ctx)
        timeout = aiohttp.ClientTimeout(total=_EXCHANGE_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
        url = f"https://{endpoint.host}:{endpoint.port}/api/v1/auth/logout"
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            await session.post(
                url, headers={"Cookie": f"{_SESSION_COOKIE_NAME}={session_cookie}"}
            )
    except (TimeoutError, aiohttp.ClientError, CompanionUnreachableError) as exc:
        logger.info("hermes.ads_bridge.upstream_logout_failed reason=%s", type(exc).__name__)
