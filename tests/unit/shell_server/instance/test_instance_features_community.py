"""Unit tests — Inc 5' (2026-07-07): `agentes` absent from Community's granted views.

Coverage:
  - `_community_views()` excludes `agentes`, keeps every other `_ALL_VIEWS` entry.
  - GET /api/v1/instance/features on a community instance excludes `agentes`.
  - GET /api/v1/instance/features on an associate instance is unchanged (views
    travel via license.views, `agentes` included when the cloud grants it).
  - The community fail-soft error path also excludes `agentes` (consistency —
    a storage error must not resurrect a module that was removed).
  - `tablero`/`chat` always-on discipline is untouched (tablero never enters
    _ALL_VIEWS at all; chat is never excluded).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.instance.api import (
    _ALL_VIEWS,
    _COMMUNITY_EXCLUDED_VIEWS,
    _community_views,
    create_instance_router,
)

pytestmark = pytest.mark.unit


class TestCommunityViewsHelper:
    def test_agentes_excluded(self) -> None:
        assert "agentes" not in _community_views()

    def test_every_other_view_kept(self) -> None:
        views = _community_views()
        for v in _ALL_VIEWS:
            if v not in _COMMUNITY_EXCLUDED_VIEWS:
                assert v in views, f"expected {v!r} to survive the community filter"

    def test_exact_set(self) -> None:
        assert set(_community_views()) == set(_ALL_VIEWS) - _COMMUNITY_EXCLUDED_VIEWS

    def test_chat_never_excluded(self) -> None:
        assert "chat" in _community_views()


def _build_store(edition: str, views: list[str] | None = None) -> MagicMock:
    store = MagicMock()
    store.edition.return_value = edition
    store.is_associated.return_value = edition == "associate"
    if edition == "associate":
        assoc = MagicMock()
        assoc.license = {"views": views or []}
        store.get.return_value = assoc
    else:
        store.get.return_value = None
    return store


@pytest.fixture
def app() -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.include_router(
        create_instance_router(db_path=Path("/nonexistent/test.db"), vault=MagicMock())
    )
    return fastapi_app


class TestInstanceFeaturesCommunity:
    def test_community_excludes_agentes(self, app: FastAPI) -> None:
        store = _build_store("community")
        with patch(
            "hermes.shell_server.instance.api.SQLiteAssociationStore",
            return_value=store,
        ):
            with TestClient(app) as client:
                resp = client.get("/api/v1/instance/features")
        assert resp.status_code == 200
        body = resp.json()
        assert body["edition"] == "community"
        assert "agentes" not in body["views"]
        # every other canonical view survives — this is a subtraction, not a
        # ground-up allowlist rewrite.
        assert set(_ALL_VIEWS) - {"agentes"} <= set(body["views"])

    def test_community_storage_error_still_excludes_agentes(self, app: FastAPI) -> None:
        with patch(
            "hermes.shell_server.instance.api.SQLiteAssociationStore",
            side_effect=RuntimeError("db locked"),
        ):
            with TestClient(app) as client:
                resp = client.get("/api/v1/instance/features")
        assert resp.status_code == 200
        body = resp.json()
        assert body["edition"] == "community"
        assert "agentes" not in body["views"]

    def test_associate_path_unchanged_agentes_travels_via_license(
        self, app: FastAPI
    ) -> None:
        store = _build_store("associate", views=["chat", "coste", "agentes"])
        with patch(
            "hermes.shell_server.instance.api.SQLiteAssociationStore",
            return_value=store,
        ):
            with TestClient(app) as client:
                resp = client.get("/api/v1/instance/features")
        assert resp.status_code == 200
        body = resp.json()
        assert body["edition"] == "associate"
        # Associate is untouched by the community subtraction — the cloud's
        # granted set (which may or may not include agentes) is authoritative.
        assert body["views"] == ["chat", "coste", "agentes"]

    def test_associate_without_agentes_grant_stays_absent(self, app: FastAPI) -> None:
        store = _build_store("associate", views=["chat", "coste"])
        with patch(
            "hermes.shell_server.instance.api.SQLiteAssociationStore",
            return_value=store,
        ):
            with TestClient(app) as client:
                resp = client.get("/api/v1/instance/features")
        body = resp.json()
        assert "agentes" not in body["views"]
