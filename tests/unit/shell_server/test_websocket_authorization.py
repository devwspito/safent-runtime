"""WebSocket authorization — `/api/v1/watch/agent/live` and `/api/v1/vnc`.

Starlette's `@app.middleware("http")` (`_require_operator_token` in main.py)
NEVER runs for `scope["type"] == "websocket"` — only for `"http"`. Before this
fix `watch_live.py` and `vnc_proxy.py` each re-implemented their own token
check (imported `_verify_token` from `training_live.py`, validated ONLY the
webui session bearer) — a second, weaker source of truth than the HTTP gate,
which accepts EITHER the operator token OR the webui bearer.

`authenticate_websocket()` (main.py) is now the ONE per-connection gate both
routes call, reusing `_bearer_is_valid` — the exact same check
`_require_operator_token` runs. This file proves:
  - unauthenticated connect is refused (1008) for both routes,
  - a token via the wrong route does not bypass anything (still 1008),
  - authenticated connect (either credential) is accepted,
  - EVERY WebSocket route under /api/v1/* uses the shared authenticator
    (structural sweep).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.routing import APIWebSocketRoute

pytestmark = pytest.mark.unit

async def _first_ws_message(
    app: Any, path: str, *, timeout_s: float = 3.0
) -> dict[str, Any]:
    """Drive the ASGI app directly through a WebSocket handshake and return
    the FIRST message the app sends back (`websocket.accept` or
    `websocket.close`), then cancel the handler task immediately.

    Mirrors `_first_response_status` in test_api_v1_authorization.py: routes
    that pass authentication go on to do real work (spawn a browser, open a
    TCP RFB connection) that must never run in a unit test — cancelling the
    instant we observe the handshake outcome keeps this hermetic and fast
    regardless of what the route does after accept().
    """
    received: dict[str, Any] = {}
    got_first = asyncio.Event()
    sent_connect = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_connect
        if not sent_connect:
            sent_connect = True
            return {"type": "websocket.connect"}
        await asyncio.sleep(3600)  # never fires again within the bounded wait
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        if not received:
            received.update(message)
            got_first.set()

    bare_path, _, query = path.partition("?")
    scope = {
        "type": "websocket",
        "path": bare_path,
        "raw_path": bare_path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [],
        "client": ("test", 0),
        "server": ("test", 80),
        "scheme": "ws",
        "subprotocols": [],
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(got_first.wait(), timeout=timeout_s)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return received


def _api_v1_websocket_routes(app: Any) -> list[str]:
    from tests.route_inventory import route_inventory

    return [
        path
        for route, path in route_inventory(app.routes)
        if isinstance(route, APIWebSocketRoute) and path.startswith("/api/v1/")
    ]


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Same bootstrap pattern as test_api_v1_authorization.py's `app` fixture."""
    spool = tmp_path / "audit-spool"
    spool.mkdir()
    monkeypatch.setenv("HERMES_SHELL_DB", str(tmp_path / "shell-state.db"))
    monkeypatch.setenv("HERMES_AUDIT_SPOOL_DIR", str(spool))

    master_key = os.urandom(32)
    from hermes.shell_server import main as shell_main

    original_vault = shell_main.SecretsVault

    class _TestVault(original_vault):  # type: ignore[valid-type]
        def __init__(self, **_: Any) -> None:
            super().__init__(master_key=master_key)

    monkeypatch.setattr(shell_main, "SecretsVault", _TestVault)

    from hermes.shell_server.main import create_app

    return create_app()


def _stub_out_browser_bring_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful auth handshake, `watch_agent_live` goes on to launch
    a REAL Playwright driver (`async_playwright().start()`), which hangs
    indefinitely in this sandbox (no browser/driver available) — that is a
    behavior this test suite has no interest in exercising; only the auth
    gate does. `try_ensure_browser_running` is looked up on the module at
    CALL time (not closed over), so patching it here — after the `app`
    fixture already built the app — still takes effect; raising makes the
    handler's own `except Exception` short-circuit before it ever reaches
    Playwright.
    """
    from hermes.shell_server.cowork import watch_live as watch_live_module

    async def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("no jailed browser in this test process")

    monkeypatch.setattr(watch_live_module, "try_ensure_browser_running", _raise)


class TestWatchAgentLiveRequiresAuth:
    async def test_unauthenticated_connect_is_refused(self, app: Any) -> None:
        msg = await _first_ws_message(app, "/api/v1/watch/agent/live")
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008

    async def test_authenticated_connect_with_session_token_is_accepted(
        self, app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_out_browser_bring_up(monkeypatch)
        token = app.state.mint_session_token()
        msg = await _first_ws_message(app, f"/api/v1/watch/agent/live?token={token}")
        assert msg["type"] == "websocket.accept"

    async def test_authenticated_connect_with_operator_token_is_accepted(
        self, app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_out_browser_bring_up(monkeypatch)
        token = app.state.shell_auth_token
        msg = await _first_ws_message(app, f"/api/v1/watch/agent/live?token={token}")
        assert msg["type"] == "websocket.accept"

    async def test_garbage_token_is_refused(self, app: Any) -> None:
        msg = await _first_ws_message(
            app, "/api/v1/watch/agent/live?token=not-a-real-token"
        )
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008


class TestVncRequiresAuth:
    async def test_unauthenticated_connect_is_refused(self, app: Any) -> None:
        msg = await _first_ws_message(app, "/api/v1/vnc")
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008

    async def test_authenticated_connect_with_session_token_is_accepted(
        self, app: Any
    ) -> None:
        token = app.state.mint_session_token()
        msg = await _first_ws_message(app, f"/api/v1/vnc?token={token}")
        assert msg["type"] == "websocket.accept"

    async def test_authenticated_connect_with_operator_token_is_accepted(
        self, app: Any
    ) -> None:
        token = app.state.shell_auth_token
        msg = await _first_ws_message(app, f"/api/v1/vnc?token={token}")
        assert msg["type"] == "websocket.accept"


class TestTokenFromAnotherRouteDoesNotBypassAuth:
    """A valid bearer is a valid bearer regardless of WHERE it was minted —
    but a token minted for one WS route must not accidentally short-circuit
    another route's independent auth call; each route re-validates its OWN
    `?token=`, there is no shared per-connection state to smuggle across."""

    async def test_valid_session_token_still_required_per_connection(
        self, app: Any
    ) -> None:
        # A connect with NO token to vnc must fail even though a session token
        # exists and is valid elsewhere in the same app/process.
        _ = app.state.mint_session_token()
        msg = await _first_ws_message(app, "/api/v1/vnc")
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008

    async def test_task_id_shaped_token_does_not_authenticate(self, app: Any) -> None:
        """A well-formed-looking but unrelated value (a random UUID, not a
        minted bearer) never authenticates — sanity check against accidental
        prefix/substring matching in the comparison."""
        msg = await _first_ws_message(app, f"/api/v1/vnc?token={uuid4()}")
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008


class TestEveryApiV1WebsocketRouteUsesTheSharedAuthenticator:
    """Structural: walk the LIVE websocket routes under /api/v1/* (not a
    hand-written list) so a new WS route that forgets to call
    `authenticate_websocket()` fails this test instead of shipping unauth'd.

    """

    def test_known_websocket_routes_are_exactly_the_expected_set(
        self, app: Any
    ) -> None:
        routes = set(_api_v1_websocket_routes(app))
        assert routes == {
            "/api/v1/watch/agent/live",
            "/api/v1/vnc",
        }, (
            "A WebSocket route was added to or removed from /api/v1/* — update "
            "this sweep (and, if added, make sure it calls authenticate_websocket())."
        )

    async def test_every_route_rejects_unauthenticated(
        self, app: Any
    ) -> None:
        failures: list[str] = []
        for path in _api_v1_websocket_routes(app):
            concrete = path.replace("{session_id}", "x")
            msg = await _first_ws_message(app, concrete)
            if not (msg.get("type") == "websocket.close" and msg.get("code") == 1008):
                failures.append(f"{path} -> {msg}")
        assert not failures, (
            "WebSocket route(s) under /api/v1/* did NOT reject an unauthenticated "
            "connect with policy code 1008 — did a new route skip "
            "authenticate_websocket()?\n" + "\n".join(failures)
        )
