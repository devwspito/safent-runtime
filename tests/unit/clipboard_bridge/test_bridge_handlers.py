"""Tests — Hermes Clipboard Bridge HTTP handlers.

Uses a FakeClipboardBackend injected through the make_handler() factory so no
real Wayland session or subprocess is involved.

Coverage:
  T-CB-01  POST /clipboard stores text — GET returns it.
  T-CB-02  POST /clipboard with text at exactly the size cap → 200 OK.
  T-CB-03  POST /clipboard with text exceeding the size cap → 413.
  T-CB-04  POST /clipboard with Content-Length header exceeding cap → 413.
  T-CB-05  POST /clipboard with invalid JSON → 400.
  T-CB-06  POST /clipboard with missing "text" field → 400.
  T-CB-07  GET /clipboard when backend raises ClipboardError → {"text": ""} not 500.
  T-CB-08  POST /clipboard when backend raises ClipboardError → 503 not 500.
  T-CB-09  OPTIONS /clipboard → 204 with CORS headers.
  T-CB-10  GET /unknown → 404.
  T-CB-11  POST /unknown → 404.
  T-CB-12  Clipboard content is never logged (log records inspected).
  T-CB-13  GET /clipboard when clipboard is empty → {"text": ""}.
  T-CB-14  POST /clipboard with non-string "text" → 400.
"""

from __future__ import annotations

import io
import json
import logging
from http.client import HTTPResponse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes.clipboard_bridge.server import MAX_CLIPBOARD_BYTES, make_handler
from hermes.clipboard_bridge.wayland_backend import ClipboardError


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class FakeClipboardBackend:
    """In-memory clipboard for unit tests — no wl-copy / Wayland involved."""

    def __init__(self, *, initial: str = "", raise_on_write: bool = False, raise_on_read: bool = False) -> None:
        self._text = initial
        self._raise_on_write = raise_on_write
        self._raise_on_read = raise_on_read
        self.write_calls: list[str] = []
        self.read_calls: int = 0

    def write(self, text: str) -> None:
        self.write_calls.append(text)
        if self._raise_on_write:
            raise ClipboardError("fake write failure")
        self._text = text

    def read(self) -> str:
        self.read_calls += 1
        if self._raise_on_read:
            raise ClipboardError("fake read failure")
        return self._text


# ---------------------------------------------------------------------------
# HTTP test harness
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Minimal socket-like object for BaseHTTPRequestHandler."""

    def __init__(self, raw_request: bytes) -> None:
        self._in = io.BytesIO(raw_request)
        self._out = io.BytesIO()

    def makefile(self, mode: str, *_: Any, **__: Any) -> Any:
        if "r" in mode:
            return io.BufferedReader(self._in)  # type: ignore[arg-type]
        return self._out

    def sendall(self, data: bytes) -> None:
        self._out.write(data)

    def getpeername(self) -> tuple[str, int]:
        return ("127.0.0.1", 0)

    def response_bytes(self) -> bytes:
        self._out.seek(0)
        return self._out.read()


def _make_request(method: str, path: str, body: bytes = b"", content_type: str = "application/json") -> bytes:
    headers = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if body:
        headers += f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
    headers += "\r\n"
    return headers.encode() + body


def _send(handler_class: type, method: str, path: str, body: bytes = b"") -> dict[str, Any]:
    """Send a request through the handler and return {status, headers, body}."""
    raw = _make_request(method, path, body)
    sock = _FakeSocket(raw)
    # BaseHTTPRequestHandler reads from makefile and writes to the socket.
    with patch("hermes.clipboard_bridge.server.logger"):
        handler_class(sock, ("127.0.0.1", 0), None)  # type: ignore[arg-type]
    response_raw = sock.response_bytes()
    # Parse HTTP/1.0 response manually.
    header_part, _, body_part = response_raw.partition(b"\r\n\r\n")
    lines = header_part.decode(errors="replace").splitlines()
    status_code = int(lines[0].split(" ")[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ": " in line:
            k, _, v = line.partition(": ")
            headers[k.lower()] = v
    body_json: dict[str, Any] = {}
    if body_part:
        try:
            body_json = json.loads(body_part)
        except json.JSONDecodeError:
            body_json = {"_raw": body_part.decode(errors="replace")}
    return {"status": status_code, "headers": headers, "body": body_json}


def _post_clipboard(handler_class: type, payload: Any) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    return _send(handler_class, "POST", "/clipboard", body)


def _get_clipboard(handler_class: type) -> dict[str, Any]:
    return _send(handler_class, "GET", "/clipboard")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.unit


class TestPostClipboard:
    def test_stores_and_returns_text(self) -> None:
        """T-CB-01: round-trip — POST stores, GET returns same text."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)

        resp_post = _post_clipboard(handler, {"text": "hello secret"})
        assert resp_post["status"] == 200
        assert resp_post["body"]["ok"] is True
        assert backend.write_calls == ["hello secret"]

        backend._text = "hello secret"
        resp_get = _get_clipboard(handler)
        assert resp_get["status"] == 200
        assert resp_get["body"]["text"] == "hello secret"

    def test_exactly_at_size_cap_succeeds(self) -> None:
        """T-CB-02: raw body of exactly MAX_CLIPBOARD_BYTES → 200.

        The size cap is on the raw request body bytes.  We build a JSON payload
        whose total encoded length is exactly MAX_CLIPBOARD_BYTES — the text
        content fills the remaining bytes after the JSON envelope.
        """
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        # {"text": ""} is 11 bytes.  Fill the rest with 'A'.
        envelope_overhead = len(b'{"text": ""}')
        text = "A" * (MAX_CLIPBOARD_BYTES - envelope_overhead)
        body_bytes = json.dumps({"text": text}).encode("utf-8")
        assert len(body_bytes) == MAX_CLIPBOARD_BYTES
        raw = (
            f"POST /clipboard HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body_bytes)}\r\n\r\n"
        ).encode() + body_bytes
        sock = _FakeSocket(raw)
        with patch("hermes.clipboard_bridge.server.logger"):
            handler(sock, ("127.0.0.1", 0), None)  # type: ignore[arg-type]
        resp_raw = sock.response_bytes()
        header_part, _, _ = resp_raw.partition(b"\r\n\r\n")
        status = int(header_part.decode().splitlines()[0].split(" ")[1])
        assert status == 200

    def test_exceeds_size_cap_by_content_length_returns_413(self) -> None:
        """T-CB-03 / T-CB-04: body > cap → 413 Payload Too Large.

        A text value that encodes to more than MAX_CLIPBOARD_BYTES bytes
        must be rejected even if it somehow slips past the Content-Length
        pre-check.
        """
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        # One byte over — the encoded text value itself exceeds the cap.
        oversized = "B" * (MAX_CLIPBOARD_BYTES + 1)
        resp = _post_clipboard(handler, {"text": oversized})
        assert resp["status"] == 413
        assert resp["body"]["error"] == "payload_too_large"
        assert len(backend.write_calls) == 0

    def test_invalid_json_returns_400(self) -> None:
        """T-CB-05: malformed JSON body → 400."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        raw = _make_request("POST", "/clipboard", b"not-json")
        sock = _FakeSocket(raw)
        with patch("hermes.clipboard_bridge.server.logger"):
            handler(sock, ("127.0.0.1", 0), None)  # type: ignore[arg-type]
        resp_raw = sock.response_bytes()
        header_part, _, body_part = resp_raw.partition(b"\r\n\r\n")
        status = int(header_part.decode().splitlines()[0].split(" ")[1])
        body = json.loads(body_part)
        assert status == 400
        assert body["error"] == "invalid_json"

    def test_missing_text_field_returns_400(self) -> None:
        """T-CB-06: body without "text" → 400."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        resp = _post_clipboard(handler, {"data": "oops"})
        assert resp["status"] == 400
        assert resp["body"]["error"] == "field_text_required"

    def test_non_string_text_returns_400(self) -> None:
        """T-CB-14: body with non-string "text" → 400."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        resp = _post_clipboard(handler, {"text": 42})
        assert resp["status"] == 400
        assert resp["body"]["error"] == "field_text_required"

    def test_backend_error_returns_503_not_500(self) -> None:
        """T-CB-08: ClipboardError from backend → 503 (bridge unavailable), not 500."""
        backend = FakeClipboardBackend(raise_on_write=True)
        handler = make_handler(backend)
        resp = _post_clipboard(handler, {"text": "hello"})
        assert resp["status"] == 503
        assert resp["body"]["error"] == "clipboard_unavailable"


class TestGetClipboard:
    def test_returns_empty_string_on_empty_clipboard(self) -> None:
        """T-CB-13: empty clipboard → {"text": ""} not an error."""
        backend = FakeClipboardBackend(initial="")
        handler = make_handler(backend)
        resp = _get_clipboard(handler)
        assert resp["status"] == 200
        assert resp["body"]["text"] == ""

    def test_backend_error_returns_empty_string_not_500(self) -> None:
        """T-CB-07: ClipboardError on GET → {"text": ""} not 500."""
        backend = FakeClipboardBackend(raise_on_read=True)
        handler = make_handler(backend)
        resp = _get_clipboard(handler)
        assert resp["status"] == 200
        assert resp["body"]["text"] == ""


class TestCors:
    def test_options_returns_204_with_cors_headers(self) -> None:
        """T-CB-09: OPTIONS preflight → 204 with CORS headers."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        raw = _make_request("OPTIONS", "/clipboard")
        sock = _FakeSocket(raw)
        with patch("hermes.clipboard_bridge.server.logger"):
            handler(sock, ("127.0.0.1", 0), None)  # type: ignore[arg-type]
        resp_raw = sock.response_bytes()
        header_part, _, _ = resp_raw.partition(b"\r\n\r\b")
        lines = header_part.decode(errors="replace").splitlines()
        status = int(lines[0].split(" ")[1])
        headers = {k.lower(): v for line in lines[1:] if ": " in line for k, v in [line.split(": ", 1)]}
        assert status == 204
        assert headers.get("access-control-allow-origin") == "*"
        assert "POST" in headers.get("access-control-allow-methods", "")

    def test_post_response_includes_cors_origin_header(self) -> None:
        """CORS header present on POST responses."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        resp = _post_clipboard(handler, {"text": "x"})
        assert resp["headers"].get("access-control-allow-origin") == "*"

    def test_get_response_includes_cors_origin_header(self) -> None:
        """CORS header present on GET responses."""
        backend = FakeClipboardBackend(initial="y")
        handler = make_handler(backend)
        resp = _get_clipboard(handler)
        assert resp["headers"].get("access-control-allow-origin") == "*"


class TestRouting:
    def test_get_unknown_path_returns_404(self) -> None:
        """T-CB-10: GET /unknown → 404."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        resp = _send(handler, "GET", "/unknown")
        assert resp["status"] == 404

    def test_post_unknown_path_returns_404(self) -> None:
        """T-CB-11: POST /unknown → 404."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        resp = _send(handler, "POST", "/unknown", json.dumps({"text": "x"}).encode())
        assert resp["status"] == 404


class TestContentNeverLogged:
    """T-CB-12: clipboard content must never appear in log records."""

    def _collect_logs(self, level: int = logging.DEBUG) -> list[logging.LogRecord]:
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("hermes-clipboard-bridge")
        handler = _Capture(level)
        logger.addHandler(handler)
        logger.setLevel(level)
        yield records
        logger.removeHandler(handler)

    def test_post_does_not_log_text_content(self, caplog: pytest.LogCaptureFixture) -> None:
        secret = "sk-supersecret-api-key-12345"
        backend = FakeClipboardBackend()
        handler = make_handler(backend)
        with caplog.at_level(logging.DEBUG, logger="hermes-clipboard-bridge"):
            _post_clipboard(handler, {"text": secret})
        for record in caplog.records:
            full = record.getMessage() + str(getattr(record, "__dict__", {}))
            assert secret not in full, (
                f"Secret appeared in log record: {record.getMessage()!r}"
            )

    def test_get_does_not_log_text_content(self, caplog: pytest.LogCaptureFixture) -> None:
        secret = "ghp_mysupersecrettoken"
        backend = FakeClipboardBackend(initial=secret)
        handler = make_handler(backend)
        with caplog.at_level(logging.DEBUG, logger="hermes-clipboard-bridge"):
            _get_clipboard(handler)
        for record in caplog.records:
            full = record.getMessage() + str(getattr(record, "__dict__", {}))
            assert secret not in full, (
                f"Secret appeared in log record: {record.getMessage()!r}"
            )


class TestContentLengthSizeCap:
    """Ensure the Content-Length header cap fires before reading the body."""

    def test_large_content_length_header_returns_413_without_reading_body(self) -> None:
        """T-CB-04: oversized Content-Length header → 413, body never fully read."""
        backend = FakeClipboardBackend()
        handler = make_handler(backend)

        oversized_cl = MAX_CLIPBOARD_BYTES + 1
        # Send a tiny actual body — the check must fire on Content-Length, not body.
        tiny_body = b'{"text": "x"}'
        raw = (
            f"POST /clipboard HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Type: application/json\r\nContent-Length: {oversized_cl}\r\n\r\n"
        ).encode() + tiny_body
        sock = _FakeSocket(raw)
        with patch("hermes.clipboard_bridge.server.logger"):
            handler(sock, ("127.0.0.1", 0), None)  # type: ignore[arg-type]
        resp_raw = sock.response_bytes()
        header_part, _, body_part = resp_raw.partition(b"\r\n\r\n")
        status = int(header_part.decode().splitlines()[0].split(" ")[1])
        assert status == 413
        assert len(backend.write_calls) == 0
