"""Tests 4 & 5: Security invariants.

test_security_no_key_leak — repr of ResolvedModel and HermesCliRequest must
  not contain the plaintext API key under any circumstances.

test_vault_resolver_single_decrypt_point — only VaultProviderResolver._build
  calls reveal_api_key. Every other resolver method delegates to _build.
  We verify this by inspecting the source code of VaultProviderResolver.
"""

from __future__ import annotations

import inspect

import pytest

from hermes.providers.domain.catalog import canonical_for
from hermes.providers.domain.ports import HermesCliRequest, ResolvedModel
from hermes.providers.infrastructure.nous_provider_adapter import nous_request_from_resolved
from hermes.providers.infrastructure.vault_provider_resolver import VaultProviderResolver
from hermes.shell_server.providers.domain import ProviderKind, Provider, ProviderConnectivity

from datetime import UTC, datetime
from uuid import uuid4


def _make_resolved(api_key: str) -> ResolvedModel:
    canonical = canonical_for(ProviderKind.OPENAI)
    provider = Provider(
        provider_id=uuid4(),
        alias="leak-test",
        kind=ProviderKind.OPENAI,
        base_url=None,
        has_api_key=True,
        default_model="gpt-4o",
        enabled=True,
        is_active=True,
        connectivity=ProviderConnectivity.UNKNOWN,
        created_at=datetime.now(tz=UTC),
    )
    return ResolvedModel(
        provider=provider,
        canonical=canonical,
        api_key=api_key,
        base_url="https://api.openai.com/v1",
    )


class TestSecurityNoKeyLeak:
    _SENTINEL_KEY = "sk-SUPER_SECRET_SENTINEL_VALUE_12345"

    def test_resolved_model_repr_does_not_contain_key(self) -> None:
        resolved = _make_resolved(self._SENTINEL_KEY)
        assert self._SENTINEL_KEY not in repr(resolved), (
            "api_key leaked in ResolvedModel repr — "
            "must be redacted to prevent log/trace leakage."
        )

    def test_resolved_model_str_does_not_contain_key(self) -> None:
        resolved = _make_resolved(self._SENTINEL_KEY)
        assert self._SENTINEL_KEY not in str(resolved)

    def test_hermes_cli_request_repr_does_not_contain_key(self) -> None:
        req = HermesCliRequest(
            requested="openrouter",
            explicit_api_key=self._SENTINEL_KEY,
            explicit_base_url="https://api.openai.com/v1",
        )
        assert self._SENTINEL_KEY not in repr(req), (
            "explicit_api_key leaked in HermesCliRequest repr."
        )

    def test_nous_request_from_resolved_repr_does_not_contain_key(self) -> None:
        resolved = _make_resolved(self._SENTINEL_KEY)
        req = nous_request_from_resolved(resolved)
        assert self._SENTINEL_KEY not in repr(req)

    def test_resolved_model_repr_shows_set_for_present_key(self) -> None:
        resolved = _make_resolved(self._SENTINEL_KEY)
        assert "<set>" in repr(resolved)

    def test_resolved_model_repr_shows_unset_for_none_key(self) -> None:
        resolved = _make_resolved.__wrapped__(None) if hasattr(_make_resolved, '__wrapped__') else None
        # Build manually with None key
        canonical = canonical_for(ProviderKind.ANTHROPIC)
        provider = Provider(
            provider_id=uuid4(),
            alias="no-key",
            kind=ProviderKind.ANTHROPIC,
            base_url=None,
            has_api_key=False,
            default_model="claude-3-haiku",
            enabled=True,
            is_active=False,
            connectivity=ProviderConnectivity.UNKNOWN,
            created_at=datetime.now(tz=UTC),
        )
        resolved_no_key = ResolvedModel(
            provider=provider,
            canonical=canonical,
            api_key=None,
            base_url=None,
        )
        assert "<unset>" in repr(resolved_no_key)


class TestVaultResolverSingleDecryptPoint:
    def test_reveal_api_key_only_called_in_build(self) -> None:
        """Only _build() may call reveal_api_key. Other methods must not.

        Inspects the source of VaultProviderResolver to verify that
        reveal_api_key() is called ONLY inside _build(). If someone adds
        a new method that calls reveal_api_key() directly (bypassing the
        single-decrypt-point invariant), this test fails loudly.
        """
        source = inspect.getsource(VaultProviderResolver)
        lines = source.splitlines()

        # Find all lines containing 'reveal_api_key'
        reveal_lines = [(i + 1, line.strip()) for i, line in enumerate(lines) if "reveal_api_key" in line]

        # All such lines must be inside the _build method.
        # We check this by ensuring every reference is either the _build
        # signature context or actually inside _build's indented block.
        # Simple heuristic: every 'reveal_api_key' call must be on a line
        # that is indented under 'def _build'.
        in_build_block = False
        build_indent: int | None = None
        in_string_literal = False
        violations: list[tuple[int, str]] = []

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # Track triple-quoted string literals (docstrings).
            triple_count = line.count('"""') + line.count("'''")
            if triple_count % 2 != 0:
                in_string_literal = not in_string_literal

            if stripped.startswith("def _build("):
                in_build_block = True
                build_indent = indent
                continue

            if in_build_block:
                # Exit block when we hit another def/class at same or shallower indent
                if stripped.startswith(("def ", "class ")) and indent <= (build_indent or 0):
                    in_build_block = False
                    build_indent = None

            # Skip comments and lines inside docstrings/string literals.
            is_comment = stripped.startswith("#")
            if "reveal_api_key()" in line and not is_comment and not in_string_literal:
                if not in_build_block:
                    violations.append((i + 1, line.strip()))

        assert violations == [], (
            "reveal_api_key() called outside _build() in VaultProviderResolver. "
            f"Violations at lines: {violations}. "
            "This breaks the single-decrypt-point security invariant."
        )

    def test_resolver_protocol_compliant(self) -> None:
        """VaultProviderResolver implements the ProviderResolver protocol methods."""
        from hermes.providers.domain.ports import ProviderResolver

        assert hasattr(VaultProviderResolver, "resolve_active")
        assert hasattr(VaultProviderResolver, "resolve_provider")
        assert hasattr(VaultProviderResolver, "canonical_for")
        # Runtime check via protocol (runtime_checkable)
        # We can't instantiate without a repo, but we can check method presence.
        for method in ("resolve_active", "resolve_provider", "canonical_for"):
            assert callable(getattr(VaultProviderResolver, method))
