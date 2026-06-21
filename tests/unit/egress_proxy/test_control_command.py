"""Tests del parser de comandos de control del socket root.

Cubre:
  - Parseo válido open-logged (sin dominios).
  - Parseo válido default-deny con dominios.
  - JSON inválido → ControlCommandError.
  - Falta session_id → ControlCommandError.
  - Modo inválido → ControlCommandError.
  - domains no es lista → ControlCommandError.
  - Dominios normalizados a minúsculas.
"""

from __future__ import annotations

import json

import pytest

from hermes.egress_proxy.application.control_command import (
    ControlCommandError,
    parse_control_command,
)
from hermes.egress_proxy.domain.policy import EgressMode

pytestmark = pytest.mark.unit


class TestParseControlCommandHappyPath:
    def test_open_logged_no_domains(self) -> None:
        raw = json.dumps({"session_id": "sess-1", "mode": "open-logged"})
        policy = parse_control_command(raw)
        assert policy.session_id == "sess-1"
        assert policy.mode == EgressMode.OPEN_LOGGED
        assert len(policy.domains_whitelist) == 0

    def test_default_deny_with_domains(self) -> None:
        raw = json.dumps(
            {
                "session_id": "sess-2",
                "mode": "default-deny",
                "domains": ["example.com", "api.example.com"],
            }
        )
        policy = parse_control_command(raw)
        assert policy.session_id == "sess-2"
        assert policy.mode == EgressMode.DEFAULT_DENY
        assert "example.com" in policy.domains_whitelist
        assert "api.example.com" in policy.domains_whitelist

    def test_domains_normalized_lowercase(self) -> None:
        raw = json.dumps(
            {
                "session_id": "sess-3",
                "mode": "default-deny",
                "domains": ["EXAMPLE.COM", "Api.Example.COM"],
            }
        )
        policy = parse_control_command(raw)
        assert "example.com" in policy.domains_whitelist
        assert "api.example.com" in policy.domains_whitelist

    def test_empty_domains_list(self) -> None:
        raw = json.dumps(
            {"session_id": "sess-4", "mode": "default-deny", "domains": []}
        )
        policy = parse_control_command(raw)
        assert len(policy.domains_whitelist) == 0

    def test_bytes_input(self) -> None:
        raw = b'{"session_id": "sess-5", "mode": "open-logged"}'
        policy = parse_control_command(raw)
        assert policy.session_id == "sess-5"


class TestParseControlCommandErrors:
    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ControlCommandError, match="JSON"):
            parse_control_command("not-json")

    def test_missing_session_id_raises(self) -> None:
        raw = json.dumps({"mode": "open-logged"})
        with pytest.raises(ControlCommandError, match="session_id"):
            parse_control_command(raw)

    def test_missing_mode_raises(self) -> None:
        raw = json.dumps({"session_id": "sess-x"})
        with pytest.raises(ControlCommandError, match="mode"):
            parse_control_command(raw)

    def test_invalid_mode_raises(self) -> None:
        raw = json.dumps({"session_id": "sess-x", "mode": "permissive"})
        with pytest.raises(ControlCommandError, match="Modo inválido"):
            parse_control_command(raw)

    def test_domains_not_list_raises(self) -> None:
        raw = json.dumps(
            {"session_id": "sess-x", "mode": "default-deny", "domains": "example.com"}
        )
        with pytest.raises(ControlCommandError, match="lista"):
            parse_control_command(raw)

    def test_not_object_raises(self) -> None:
        raw = json.dumps(["open-logged", "example.com"])
        with pytest.raises(ControlCommandError, match="objeto"):
            parse_control_command(raw)

    def test_empty_session_id_raises(self) -> None:
        raw = json.dumps({"session_id": "", "mode": "open-logged"})
        with pytest.raises(ControlCommandError, match="session_id"):
            parse_control_command(raw)


# ── Fuzz regression (red-team 2026-06-19) ─────────────────────────────────────
# The parser previously caught only json.JSONDecodeError, leaking RecursionError
# (deeply-nested JSON) and UnicodeDecodeError (invalid UTF-8) out of the control
# socket handler. It now normalises EVERY parse failure to ControlCommandError and
# caps the domain list.

def test_deeply_nested_json_does_not_leak_recursion_error() -> None:
    raw = b'{"a":' + b"[" * 50000 + b"}"
    with pytest.raises(ControlCommandError):
        parse_control_command(raw)


def test_invalid_utf8_does_not_leak_unicode_error() -> None:
    raw = b'{"mode":"\x00\x01","domains":[],"session_id":"\xf0\x9f"}'
    with pytest.raises(ControlCommandError):
        parse_control_command(raw)


def test_too_many_domains_rejected() -> None:
    raw = json.dumps({
        "mode": "default-deny",
        "session_id": "s",
        "domains": ["a.com"] * 100000,
    }).encode()
    with pytest.raises(ControlCommandError):
        parse_control_command(raw)


def test_valid_command_still_parses_after_hardening() -> None:
    pol = parse_control_command(
        b'{"mode":"default-deny","session_id":"s1","domains":["github.com"]}'
    )
    assert pol.session_id == "s1"
    assert "github.com" in pol.domains_whitelist
