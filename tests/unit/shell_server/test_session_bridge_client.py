"""Unit tests for SessionBridgeClient — daemon-side socket client.

Tests verify:
  - Token loading from file
  - Socket unavailable → SessionBridgeUnavailable
  - ok=False response → SessionBridgeError
  - Correct verb/args forwarded in request
  - Token missing from disk → SessionBridgeUnavailable

No real socket is opened — _roundtrip is mocked.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.capabilities.infrastructure.session_bridge_client import (
    SessionBridgeClient,
    SessionBridgeError,
    SessionBridgeUnavailable,
)

pytestmark = pytest.mark.unit

_TOKEN = "abc123"
_SOCKET = Path("/tmp/test-session-input.sock")
_TOKEN_FILE = Path("/tmp/test-session-input.token")


def _client_with_token(tmp_path: Path) -> tuple[SessionBridgeClient, Path]:
    token_file = tmp_path / "session-input.token"
    token_file.write_text(_TOKEN, encoding="utf-8")
    sock = tmp_path / "session-input.sock"
    client = SessionBridgeClient(socket_path=sock, token_path=token_file)
    return client, sock


class TestTokenLoading:
    def test_token_loaded_from_file(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        assert client._load_token() == _TOKEN

    def test_token_cached_after_first_load(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        client._load_token()
        # Delete file — should still work from cache
        (tmp_path / "session-input.token").unlink()
        assert client._load_token() == _TOKEN

    def test_missing_token_file_raises(self, tmp_path: Path) -> None:
        client = SessionBridgeClient(
            socket_path=tmp_path / "s.sock",
            token_path=tmp_path / "nonexistent.token",
        )
        with pytest.raises(SessionBridgeUnavailable, match="token not found"):
            client._load_token()


class TestSocketUnavailable:
    async def test_missing_socket_raises(self, tmp_path: Path) -> None:
        token_file = tmp_path / "t.token"
        token_file.write_text(_TOKEN, encoding="utf-8")
        client = SessionBridgeClient(
            socket_path=tmp_path / "nonexistent.sock",
            token_path=token_file,
        )
        with pytest.raises(SessionBridgeUnavailable, match="socket not found"):
            await client.screenshot()


class TestResponseHandling:
    async def _call_with_response(
        self, client: SessionBridgeClient, response: dict, verb_coro
    ) -> dict:
        """Patch _roundtrip to return a fixed response."""
        async def _fake_roundtrip(request: dict) -> dict:
            return response

        with patch.object(client, "_roundtrip", side_effect=_fake_roundtrip):
            return await verb_coro()

    async def test_ok_response_returned(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        result = await self._call_with_response(
            client, {"ok": True, "path": "/tmp/x.png"}, client.screenshot
        )
        assert result == {"ok": True, "path": "/tmp/x.png"}

    async def test_error_response_raises(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        with pytest.raises(SessionBridgeError, match="rate_limit_exceeded"):
            await self._call_with_response(
                client, {"ok": False, "error": "rate_limit_exceeded"}, client.screenshot
            )

    async def test_pointer_motion_sends_correct_verb(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        captured: dict = {}

        async def _fake_roundtrip(request: dict) -> dict:
            captured.update(request)
            return {"ok": True}

        with patch.object(client, "_roundtrip", side_effect=_fake_roundtrip):
            await client.pointer_motion(42.5, 99.0)

        assert captured["verb"] == "pointer_motion"
        assert captured["x"] == 42.5
        assert captured["y"] == 99.0
        assert captured["token"] == _TOKEN

    async def test_type_text_sends_correct_verb(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        captured: dict = {}

        async def _fake_roundtrip(request: dict) -> dict:
            captured.update(request)
            return {"ok": True}

        with patch.object(client, "_roundtrip", side_effect=_fake_roundtrip):
            await client.type_text("hello world")

        assert captured["verb"] == "type_text"
        assert captured["text"] == "hello world"

    async def test_keycode_sends_correct_verb(self, tmp_path: Path) -> None:
        client, _ = _client_with_token(tmp_path)
        captured: dict = {}

        async def _fake_roundtrip(request: dict) -> dict:
            captured.update(request)
            return {"ok": True}

        with patch.object(client, "_roundtrip", side_effect=_fake_roundtrip):
            await client.keycode(30, True)

        assert captured["verb"] == "keycode"
        assert captured["code"] == 30
        assert captured["press"] is True
