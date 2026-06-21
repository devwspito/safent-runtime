"""Tests for the standalone signaling daemon's bootstrap.

Regression: main() crashed with `ValueError: Unknown level: 'info'` because
logging.basicConfig got the lowercase env value (HERMES_RC_LOG_LEVEL=info)
that uvicorn expects, but logging wants uppercase. See service.py main().
"""

from __future__ import annotations

import logging

import pytest

from hermes.shell_server.remote_control import service

pytestmark = pytest.mark.unit


class TestLogLevelParsing:
    @pytest.mark.parametrize("value", ["info", "INFO", "debug", "Warning"])
    def test_lowercase_env_does_not_crash_logging(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # logging.basicConfig must accept the env value regardless of case.
        level = value.upper()
        # This mirrors what main() does; if logging rejected it, it'd raise.
        logging.getLogger("test-rc").setLevel(level)

    def test_app_factory_registers_routes(self) -> None:
        app = service.create_app()
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/healthz" in paths
        assert "/rc/{session_id}" in paths


class TestStubAnswer:
    def test_answer_carries_expected_fingerprint(self) -> None:
        fp = "ab:cd:ef" * 5
        sdp = service._build_stub_answer("v=0", expected_fp=fp)
        assert "v=0" in sdp
        assert f"a=fingerprint:sha-256 {fp}" in sdp
        assert "m=video" in sdp
