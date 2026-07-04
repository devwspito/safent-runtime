"""Unit tests — vnc_proxy `?session=` query param resolution (C1: concurrent
jailed-browser sessions).

Covers:
  - Missing param (None) resolves to the default session (back-compat: no
    session parameter anywhere means byte-identical behavior).
  - Valid exec-* / the exact teaching session are accepted as-is.
  - Invalid values (path traversal, empty string, uppercase, wrong prefix,
    unknown "teaching-*" variant, over-length) are REJECTED (default-deny —
    returns None, never silently substituted with the default).
"""

from __future__ import annotations

import pytest

from hermes.shell_server.cowork.vnc_proxy import (
    _DEFAULT_SESSION,
    _resolve_session_name,
)

pytestmark = pytest.mark.unit


class TestMissingParamDefaults:
    def test_none_resolves_to_default_session(self) -> None:
        assert _resolve_session_name(None) == _DEFAULT_SESSION == "exec-browse"


class TestValidSessionNamesAccepted:
    @pytest.mark.parametrize(
        "name", ["exec-browse", "exec-abc123", "exec-test77", "exec-0", "teaching-chromium"]
    )
    def test_accepted_as_is(self, name: str) -> None:
        assert _resolve_session_name(name) == name


class TestInvalidSessionNamesRejected:
    """Present-but-invalid values are REJECTED (None), never silently defaulted."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty (present, distinct from missing)
            "../../etc/passwd",  # path traversal
            "exec-abc/../evil",  # path traversal
            "teaching-x",  # wrong teaching variant (only teaching-chromium allowed)
            "EXEC-BROWSE",  # uppercase
            "Exec-Abc",  # mixed case
            "hermes-browser-exec-browse",  # wrong prefix entirely
            "browse",  # no prefix at all
            "exec-" + "a" * 300,  # excessively long
            "exec abc",  # embedded space
            "exec-abc!",  # special char
        ],
    )
    def test_rejected(self, raw: str) -> None:
        assert _resolve_session_name(raw) is None, f"expected {raw!r} to be rejected"
