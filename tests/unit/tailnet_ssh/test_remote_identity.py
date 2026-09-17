"""RemoteIdentity — format validation for a CLOUD-managed host's declared
remote login user (spec 002 US3, D-4)."""

from __future__ import annotations

import pytest

from hermes.tailnet_ssh.domain.errors import InvalidRemoteIdentityError
from hermes.tailnet_ssh.domain.remote_identity import RemoteIdentity

pytestmark = pytest.mark.unit


class TestRemoteIdentityParse:
    def test_lowercase_word_parses(self) -> None:
        assert RemoteIdentity.parse("deploy").value == "deploy"

    def test_leading_underscore_parses(self) -> None:
        assert RemoteIdentity.parse("_svc").value == "_svc"

    def test_digits_and_hyphens_after_first_char_parse(self) -> None:
        assert RemoteIdentity.parse("svc-01").value == "svc-01"

    def test_max_length_32_parses(self) -> None:
        value = "a" * 32
        assert RemoteIdentity.parse(value).value == value


class TestRemoteIdentityRejects:
    def test_non_string_rejected(self) -> None:
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse(None)

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse("")

    def test_uppercase_rejected(self) -> None:
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse("Deploy")

    def test_leading_digit_rejected(self) -> None:
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse("1deploy")

    def test_over_32_chars_rejected(self) -> None:
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse("a" * 33)

    def test_flag_shaped_value_rejected(self) -> None:
        """The defense-in-depth guard: even though the executor treats `-l`'s
        argument as one opaque token, a flag-shaped identity is still not a
        VALID identity by format — rejected before it ever reaches ssh."""
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse("-oProxyCommand=evil")

    def test_whitespace_rejected(self) -> None:
        with pytest.raises(InvalidRemoteIdentityError):
            RemoteIdentity.parse("de ploy")
