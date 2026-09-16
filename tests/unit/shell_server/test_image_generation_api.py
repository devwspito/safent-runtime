"""Image generation (FAL.ai) key REST API — unit tests. Mirrors web_search_api.

Coverage:
  - GET status proxies get_image_generation_status; fail-soft (daemon down ->
    has_key False, never 500).
  - POST /key stores the key; response is ONLY {has_key: true}, the raw key
    is never echoed back nor logged.
  - POST /key with the daemon unavailable -> 503.
  - POST /key with the daemon rejecting the write -> 422.
  - DELETE /key clears the key; response {has_key: false}.
  - DELETE /key with the daemon unavailable -> 503.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.integrations.image_generation_api import (
    create_image_generation_router,
)
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

pytestmark = pytest.mark.unit

_SECRET_KEY = "fal-super-secret-key-should-never-leak"


def _make_app(*, proxy: MagicMock) -> FastAPI:
    app = FastAPI()
    app.state.dbus_proxy = proxy
    app.include_router(create_image_generation_router())
    return app


def _proxy() -> MagicMock:
    proxy = MagicMock()
    proxy.call_dict = AsyncMock(return_value={})
    proxy.call_mutator = AsyncMock(return_value={"ok": True})
    return proxy


class TestGetStatus:
    def test_returns_daemon_status(self) -> None:
        proxy = _proxy()
        proxy.call_dict = AsyncMock(
            return_value={"provider": "fal", "has_key": True, "model": "fal-ai/flux-2/klein/9b"}
        )
        client = TestClient(_make_app(proxy=proxy))

        r = client.get("/api/v1/integrations/image-generation")

        assert r.status_code == 200
        assert r.json() == {
            "provider": "fal", "has_key": True, "model": "fal-ai/flux-2/klein/9b",
        }
        proxy.call_dict.assert_awaited_once_with("get_image_generation_status")

    def test_daemon_unavailable_fails_soft(self) -> None:
        proxy = _proxy()
        proxy.call_dict = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(proxy=proxy))

        r = client.get("/api/v1/integrations/image-generation")

        assert r.status_code == 200
        assert r.json() == {"provider": "fal", "has_key": False, "model": None}


class TestSetKey:
    def test_valid_key_returns_has_key_only(self) -> None:
        proxy = _proxy()
        client = TestClient(_make_app(proxy=proxy))

        r = client.post(
            "/api/v1/integrations/image-generation/key", json={"api_key": _SECRET_KEY}
        )

        assert r.status_code == 200
        assert r.json() == {"has_key": True}
        assert _SECRET_KEY not in r.text
        proxy.call_mutator.assert_awaited_once_with(
            "set_image_generation_api_key", _SECRET_KEY
        )

    def test_key_never_appears_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy = _proxy()
        client = TestClient(_make_app(proxy=proxy))

        with caplog.at_level(logging.DEBUG):
            client.post(
                "/api/v1/integrations/image-generation/key", json={"api_key": _SECRET_KEY}
            )

        leaked = [r.getMessage() for r in caplog.records if _SECRET_KEY in r.getMessage()]
        assert leaked == []

    def test_empty_key_rejected_by_validation(self) -> None:
        proxy = _proxy()
        client = TestClient(_make_app(proxy=proxy))

        r = client.post("/api/v1/integrations/image-generation/key", json={"api_key": ""})

        assert r.status_code == 422
        proxy.call_mutator.assert_not_awaited()

    def test_daemon_unavailable_returns_503(self) -> None:
        proxy = _proxy()
        proxy.call_mutator = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(proxy=proxy))

        r = client.post(
            "/api/v1/integrations/image-generation/key", json={"api_key": _SECRET_KEY}
        )

        assert r.status_code == 503

    def test_daemon_rejects_write_returns_422(self) -> None:
        proxy = _proxy()
        proxy.call_mutator = AsyncMock(return_value={"ok": False, "error": "boom"})
        client = TestClient(_make_app(proxy=proxy))

        r = client.post(
            "/api/v1/integrations/image-generation/key", json={"api_key": _SECRET_KEY}
        )

        assert r.status_code == 422


class TestDeleteKey:
    def test_delete_returns_has_key_false(self) -> None:
        proxy = _proxy()
        client = TestClient(_make_app(proxy=proxy))

        r = client.delete("/api/v1/integrations/image-generation/key")

        assert r.status_code == 200
        assert r.json() == {"has_key": False}
        proxy.call_mutator.assert_awaited_once_with("delete_image_generation_api_key")

    def test_daemon_unavailable_returns_503(self) -> None:
        proxy = _proxy()
        proxy.call_mutator = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(proxy=proxy))

        r = client.delete("/api/v1/integrations/image-generation/key")

        assert r.status_code == 503
