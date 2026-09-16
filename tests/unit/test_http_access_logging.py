"""HTTP logs retain forensic metadata, never callback or bootstrap query secrets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = r"""
import logging, socket, threading, time
import httpx, uvicorn
from hermes import logging_setup

logging_setup.configure_structured_logging(service="qa-shell", version="qa")
config_factory = getattr(logging_setup, "uvicorn_log_config", lambda: uvicorn.config.LOGGING_CONFIG)
async def app(scope, receive, send):
    await send({"type":"http.response.start", "status":200,
                "headers":[(b"x-qa", b"HEADER_CANARY_7291")]})
    await send({"type":"http.response.body", "body":b"ok"})
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(
    app, log_config=config_factory(), lifespan="off", access_log=True))
thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
thread.start()
deadline = time.monotonic() + 5
while not server.started and time.monotonic() < deadline:
    time.sleep(.01)
assert server.started
try:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    with httpx.Client(trust_env=False) as client:
        targets = [
            "/ads/api/v1/connections/meta/callback"
            "?code=OAUTH_CODE_7291&state=OAUTH_STATE_7291#FRAGMENT_7291",
            "/?k=BOOTSTRAP_KEY_7291",
        ]
        for target in targets:
            response = client.get(f"http://127.0.0.1:{port}" + target)
            assert response.status_code == 200
    logging.getLogger("httpx").debug("Unexpected diagnostic %s", "EXTRA_HTTPX_SECRET_7291")
    logging.getLogger("httpcore.http11").debug(
        "receive_response_headers.complete return_value=%r", {"Authorization":"AUTH_HEADER_7291"})
    # Uvicorn's real five-argument formatter: fragment-only target is stripped too.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET",
        "/callback#DIRECT_FRAGMENT_7291", "1.1", 204)
finally:
    server.should_exit = True
    thread.join(5)
    sock.close()
assert not thread.is_alive()
"""


def test_uvicorn_reconfiguration_and_real_httpx_never_log_query_secrets() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "NO_COLOR": "1"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    for canary in [
        "OAUTH_CODE_7291",
        "OAUTH_STATE_7291",
        "FRAGMENT_7291",
        "BOOTSTRAP_KEY_7291",
        "HEADER_CANARY_7291",
        "EXTRA_HTTPX_SECRET_7291",
        "AUTH_HEADER_7291",
        "DIRECT_FRAGMENT_7291",
    ]:
        assert canary not in output
    assert "GET /ads/api/v1/connections/meta/callback HTTP/1.1" in output
    assert "GET / HTTP/1.1" in output
    assert "GET /callback HTTP/1.1" in output
    assert "200 OK" in output
    assert "204 No Content" in output
    assert "HTTP Request: GET http://127.0.0.1:" in output


def test_shell_entrypoint_supplies_safe_config_to_uvicorn() -> None:
    import ast

    path = Path(__file__).resolve().parents[2] / "src/hermes/shell_server/main.py"
    module = ast.parse(path.read_text())
    entry = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.func.attr == "run"
    ]
    assert len(calls) == 1
    config = next((kw.value for kw in calls[0].keywords if kw.arg == "log_config"), None)
    assert isinstance(config, ast.Call) and isinstance(config.func, ast.Name)
    assert config.func.id == "uvicorn_log_config"
    assert any(
        kw.arg == "access_log" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in calls[0].keywords
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/ads/callback?code=canary&state=canary#fragment", "/ads/callback"),
        ("/?k=canary", "/"),
        ("/callback#canary", "/callback"),
        (
            "https://user:password@example.test:8443/path?secret=value",
            "https://example.test:8443/path",
        ),
        ("http://[::1]:7517/app/?k=canary", "http://[::1]:7517/app/"),
        ("http://[invalid/canary", "[invalid HTTP target]"),
    ],
)
def test_target_redaction(target: str, expected: str) -> None:
    from hermes.logging_setup import _http_target_without_secrets

    assert _http_target_without_secrets(target) == expected


def test_configuration_does_not_mutate_uvicorn_global_defaults() -> None:
    from copy import deepcopy

    from uvicorn.config import LOGGING_CONFIG

    from hermes.logging_setup import uvicorn_log_config

    original = deepcopy(LOGGING_CONFIG)
    first = uvicorn_log_config()
    second = uvicorn_log_config()
    assert first is not second
    assert original == LOGGING_CONFIG
    assert first["handlers"]["access"]["filters"] == ["http_metadata"]
    assert first["loggers"]["uvicorn.access"]["propagate"] is False


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx", "httpcore.http11"])
def test_transport_formatter_never_appends_exception_or_stack_payload(name: str) -> None:
    import logging

    from uvicorn.logging import AccessFormatter, DefaultFormatter

    from hermes.logging_setup import HttpMetadataFilter

    message = '%s - "%s %s HTTP/%s" %d'
    args: tuple[object, ...] = ("127.0.0.1:1", "GET", "/callback?code=QUERY_CANARY", "1.1", 200)
    if name == "httpx":
        message = 'HTTP Request: %s %s "%s %d %s"'
        args = (
            "GET",
            "http://localhost/callback?state=QUERY_CANARY",
            "HTTP/1.1",
            200,
            "REMOTE_REASON_CANARY",
        )
    if name == "httpcore.http11":
        message, args = "receive_response_headers.failed exception=%r", ("QUERY_CANARY",)
    exception = ValueError("EXCEPTION_CANARY")
    record = logging.LogRecord(
        name,
        logging.ERROR,
        __file__,
        1,
        message,
        args,
        (ValueError, exception, None),
        sinfo="STACK_CANARY",
    )
    record.exc_text = "CACHED_EXCEPTION_CANARY"
    HttpMetadataFilter().filter(record)
    formatter = (
        AccessFormatter(fmt="%(request_line)s %(status_code)s", use_colors=False)
        if name == "uvicorn.access"
        else DefaultFormatter(use_colors=False)
    )
    result = formatter.format(record)
    assert "CANARY" not in result
    if name != "httpcore.http11":
        assert "GET" in result
        assert "/callback" in result
        assert "200 OK" in result
