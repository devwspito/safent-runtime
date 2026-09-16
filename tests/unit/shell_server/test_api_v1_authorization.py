"""Resource-level authorization for /api/v1/* — regression guard (radiografía §4.1).

Antes de este fix, `_require_operator_token` (main.py) sólo protegía las
peticiones POST/PUT/PATCH/DELETE: todo GET bajo /api/v1/* (cola de auditoría,
memoria, conversaciones, providers, dominios de egress, estado MFA...) se
servía a CUALQUIER llamador loopback sin credencial alguna.

Este test recorre las rutas VIVAS de `app.routes` — no una lista escrita a
mano — para que un router nuevo que se registre sin pasar por el middleware
compartido no pueda regresar en silencio: toda ruta /api/v1/* y todo método
debe rechazar una llamada sin autenticar con 401/403. No existe allow-list por
ruta: las únicas superficies que quedan fuera de /api/v1/* (healthz, metrics,
el handshake de bootstrap en `GET /`, los assets estáticos de /app/) no las
toca este middleware porque viven fuera del prefijo por diseño.

Excepción documentada: las 2 rutas WebSocket bajo /api/v1/* (watch/agent/live,
vnc) NO pasan por este middleware — Starlette sólo aplica
`@app.middleware("http")` al scope "http", nunca a "websocket" — y quedan
FUERA de este test a propósito. Ambas están cubiertas por su propio gate
por-conexión (`authenticate_websocket`, mismo `_bearer_is_valid` que este
middleware) — ver tests/unit/shell_server/test_websocket_authorization.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_PARAM_RE = re.compile(r"\{[^}]+\}")


def _concrete_path(route_path: str) -> str:
    """Sustituye cada {param} por un placeholder.

    El middleware de autorización corta ANTES del routing (mira sólo el
    prefijo de la URL), así que el placeholder no necesita ser un valor
    válido para el tipo del parámetro.
    """
    return _PARAM_RE.sub("x", route_path)


async def _first_response_status(
    app: Any, path: str, *, method: str = "GET", timeout_s: float = 3.0
) -> int:
    """Drive the ASGI app directly and return the `http.response.start` status.

    `/api/v1/runtime/agent-stream` streams forever by design (no natural end,
    no `request.is_disconnected()` short-circuit until the client actually
    disconnects), so anything that waits for a COMPLETE response — including
    `TestClient`'s synchronous portal, `timeout=` kwarg and all (the portal
    doesn't honor it for in-process ASGI streaming; Starlette even deprecated
    passing `timeout` to TestClient for this exact reason) — hangs forever on
    it. Starlette sends `http.response.start` BEFORE the body generator runs a
    single iteration (see `StreamingResponse.stream_response`), so driving the
    ASGI callable directly and racing it against a real `asyncio.wait_for`
    gets the status code without ever waiting for (or triggering) an infinite
    body. Used for EVERY route in the structural walk below, not just the SSE
    ones, so a future infinite/slow route can't reintroduce a hanging suite.
    """
    received: dict[str, int] = {}
    body_started = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)  # never fires within the bounded wait below
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            body_started.set()

    bare_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": bare_path,
        "raw_path": bare_path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 0),
        "server": ("test", 80),
        "scheme": "http",
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(body_started.wait(), timeout=timeout_s)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return received["status"]


def _api_v1_routes(app: Any) -> Iterator[tuple[str, str]]:
    """(método, path concreto) para cada ruta HTTP bajo /api/v1/*."""
    from tests.route_inventory import route_inventory

    for route, route_path in route_inventory(app.routes):
        if not isinstance(route, APIRoute):
            continue  # excluye las 2 rutas WebSocket — ver docstring del módulo
        if not route_path.startswith("/api/v1/"):
            continue
        path = _concrete_path(route_path)
        for method in route.methods or set():
            if method == "HEAD":
                continue  # ya cubierto por el GET correspondiente
            yield method, path


@pytest.fixture()
def app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Construye la app real vía create_app() (mismo patrón que
    tests/integration/test_chat_fail_hard.py): vault de test para no
    depender de /var/lib/hermes/master.key, DB y spool de auditoría en
    tmp_path.
    """
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


@pytest.fixture()
def client(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestEveryApiV1RouteRequiresAuth:
    """Estructural: recorre TODAS las rutas /api/v1/* vivas."""

    async def test_every_route_and_method_rejects_unauthenticated(
        self, app: Any
    ) -> None:
        routes = list(_api_v1_routes(app))
        assert routes, "no se encontraron rutas /api/v1/* — ¿cambió create_app()?"
        failures: list[str] = []
        for method, path in routes:
            # `_first_response_status` reads only the response HEAD (bounded
            # timeout) instead of the full body — a route that regresses to
            # anonymous-and-streaming (like the pre-fix /api/v1/runtime/
            # agent-stream) must FAIL this test, not hang the suite forever.
            try:
                status = await _first_response_status(app, path, method=method)
            except TimeoutError:
                failures.append(f"{method} {path} -> TIMED OUT (still streaming?)")
                continue
            if status not in (401, 403):
                failures.append(f"{method} {path} -> {status}")
        assert not failures, (
            "Rutas /api/v1/* servidas SIN credencial (deben devolver 401/403). "
            "Un router nuevo debe montarse DENTRO del grupo autorizado, nunca "
            "con una excepción propia:\n" + "\n".join(failures)
        )

    def test_route_count_sanity(self, app: Any) -> None:
        """Guarda de cordura: si esto cae mucho, algo dejó de registrarse
        (p.ej. un router condicional import-guardeado)."""
        routes = list(_api_v1_routes(app))
        assert len(routes) > 100

    def test_query_param_token_does_not_bypass_auth_on_non_sse_routes(
        self, app: Any, client: TestClient
    ) -> None:
        """El escape `?token=` es SOLO para las 2 rutas SSE (EventSource no
        puede fijar cabeceras) — en cualquier otra ruta un token por query
        string NO autentica."""
        token = app.state.mint_session_token()
        resp = client.get(f"/api/v1/providers?token={token}")
        assert resp.status_code == 401


class TestAuthenticatedSessionReachesHandlers:
    """`mint_session_token()` es EXACTAMENTE el bearer que la UI real recibe
    inyectado en `window.__SAFENT_TOKEN__` tras el handshake `?k=`. Estas
    pruebas demuestran que, con ese bearer, una muestra de rutas GET/POST
    llega al handler (status != 401) tras el endurecimiento — ni la UI ni el
    operador interno quedaron bloqueados por el cierre del hueco.
    """

    def test_get_profile_with_session_reaches_handler(
        self, app: Any, client: TestClient
    ) -> None:
        token = app.state.mint_session_token()
        resp = client.get(
            "/api/v1/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    def test_get_providers_with_session_reaches_handler(
        self, app: Any, client: TestClient
    ) -> None:
        token = app.state.mint_session_token()
        resp = client.get(
            "/api/v1/providers", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    def test_get_audit_tail_stats_with_session_reaches_handler(
        self, app: Any, client: TestClient
    ) -> None:
        token = app.state.mint_session_token()
        resp = client.get(
            "/api/v1/audit/tail/stats", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    def test_post_chat_with_operator_token_reaches_handler(
        self, app: Any, client: TestClient
    ) -> None:
        """POST autenticado con el token operador (bearer interno daemon↔shell)
        llega al handler: sin daemon D-Bus en el entorno de test, el
        ControlPlanePort lanza AgentUnavailable -> 503, NUNCA 401."""
        from hermes.tasks.control_plane.domain.ports import AgentUnavailable

        class _UnavailableControlPlane:
            async def enqueue(self, **_: Any) -> None:
                raise AgentUnavailable("sin daemon (test)")

        app.state.control_plane = _UnavailableControlPlane()
        token = app.state.shell_auth_token
        resp = client.post(
            "/api/v1/chat",
            json={"user_message": "hola"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503

    def test_sse_chat_stream_accepts_bearer_via_query_param(
        self, app: Any, client: TestClient
    ) -> None:
        """EventSource no puede fijar cabeceras: el bearer viaja como
        `?token=`. Sin socket AF_UNIX del daemon en el entorno de test, el
        generador emite un frame de error y TERMINA — no cuelga el test."""
        token = app.state.mint_session_token()
        task_id = uuid4()
        resp = client.get(f"/api/v1/chat/stream/{task_id}?token={token}")
        assert resp.status_code == 200

    async def test_sse_agent_stream_accepts_bearer_via_query_param(
        self, app: Any
    ) -> None:
        """Mismo mecanismo para el otro endpoint SSE. Este generador SÍ es
        infinito (push continuo del floor de la Office, sin condición de
        parada salvo desconexión del cliente), así que se conduce el ASGI
        directamente con un timeout acotado en vez de TestClient — ver
        `_first_response_status`."""
        token = app.state.mint_session_token()
        status = await _first_response_status(
            app, f"/api/v1/runtime/agent-stream?token={token}"
        )
        assert status == 200

    async def test_sse_agent_stream_rejects_missing_token(self, app: Any) -> None:
        """Simétrico al positivo: sin `?token=` (ni cabecera), la MISMA ruta
        SSE se rechaza con 401 — el escape de query-param no es una excepción
        de autenticación, es sólo otro transporte para el mismo bearer."""
        status = await _first_response_status(app, "/api/v1/runtime/agent-stream")
        assert status == 401


class TestRejectedAccessIsAudited:
    """Item 4: toda llamada sin autenticar a /api/v1/* deja rastro, pero
    limitado en tasa (una línea por dirección de cliente por ventana) para
    que un escáner no inunde el journal."""

    def test_first_rejection_is_logged(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="hermes-shell-server"):
            client.get("/api/v1/providers")
        hits = [
            r
            for r in caplog.records
            if r.message == "shell_http_auth.rejected_unauthenticated"
        ]
        assert len(hits) == 1

    def test_repeated_rejections_within_the_floor_are_not_re_logged(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="hermes-shell-server"):
            for _ in range(5):
                client.get("/api/v1/providers")
        hits = [
            r
            for r in caplog.records
            if r.message == "shell_http_auth.rejected_unauthenticated"
        ]
        assert len(hits) == 1, (
            "5 rechazos consecutivos del MISMO cliente deben producir 1 sola "
            f"línea de log (rate-limit), no {len(hits)} — riesgo de flood."
        )


class TestApiSurfaceListingRequiresAuth:
    """A-07 (specs/025-safent-repaso matriz-final-39eeb8e): /openapi.json was
    served 200 with NO bearer while every /api/v1/* route it describes gave
    401 — handing the full API surface (71 GET routes + the rest) to anyone
    who reaches the port. Structural, not a hardcoded path: walks every doc
    route FastAPI itself knows about (`app.openapi_url`/`docs_url`/
    `redoc_url`) so a future re-enable of /docs or /redoc can't reopen the
    same hole silently.
    """

    def test_no_doc_route_is_reachable_unauthenticated(
        self, app: Any, client: TestClient
    ) -> None:
        doc_paths = [p for p in (app.openapi_url, app.docs_url, app.redoc_url) if p]
        assert doc_paths, "openapi_url must be set — nothing to protect otherwise"
        failures = [
            f"{path} -> {resp.status_code}"
            for path in doc_paths
            if (resp := client.get(path)).status_code not in (401, 403)
        ]
        assert not failures, (
            "Doc/schema routes served WITHOUT a bearer — they hand the API "
            "surface to anyone who reaches the port:\n" + "\n".join(failures)
        )

    def test_openapi_json_reaches_the_handler_with_a_valid_bearer(
        self, app: Any, client: TestClient
    ) -> None:
        token = app.state.mint_session_token()
        resp = client.get(app.openapi_url, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["info"]["title"]
