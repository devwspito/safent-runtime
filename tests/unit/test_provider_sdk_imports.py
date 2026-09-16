"""Regression tests — specs/025-safent-repaso hallazgo #2 ("falta el SDK
anthropic (y otros): Anthropic/OpenAI-API/GLM nativos y custom mueren con
ImportError").

Root cause: hermes-agent 0.21.1 (the pinned engine, see
ops/container/Containerfile HERMES_AGENT_COMMIT) declares `anthropic`,
`boto3` (bedrock) and `azure-identity` (Azure Foundry) as OPTIONAL extras,
lazy-installed via tools/lazy_deps.py "at first use". That self-heal install
can never succeed inside Safent's hardened container: hermes-agent lives
under /usr/lib/hermes-agent, which is read-only under ProtectSystem=strict
(see ops/container/Containerfile comment on HERMES_AGENT_SRC). Safent's
providers UI offers every entry of hermes_cli.auth.PROVIDER_REGISTRY
verbatim (list_native_providers) — including anthropic/bedrock/azure-foundry
— so picking one and chatting hit a bare ImportError instead of a working
chat, or (for vertex, which imports google.auth at module top-level with no
try/except) an even uglier ModuleNotFoundError.

This file is parametrized over exactly the providers whose runtime path
lazily imports a third-party SDK not in hermes-agent's CORE dependencies
(openai is core — every OpenAI-compatible provider, i.e. openai-api/zai/
deepseek/groq/mistral/lmstudio/ollama/openrouter/…, needs nothing extra).
Skips (not fails) when hermes-agent isn't installed in this checkout — the
same convention other hermes-agent-dependent tests use (see
tests/security/test_broker_gate_hardening_iter3.py) — so it is a no-op here
in the debug-engineer worktree but has real teeth in any environment that
actually has hermes-agent installed (dev box with `pip install -e`, or a
container-build smoke step), which is exactly where "fails in CI, not in
the user's chat" (per the task) needs to hold.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit

# NOT pytest.importorskip("hermes_cli"): several OTHER unit tests (e.g.
# test_codex_provider_catalog.py, test_seeded_companion_mcp.py) permanently
# stash a BARE `types.ModuleType("hermes_cli")` into sys.modules with no
# cleanup (no monkeypatch/context-manager) as a minimal stub for their own
# narrow purpose — once any of those runs earlier in the same session,
# `import hermes_cli` "succeeds" against that empty stub for the rest of the
# run. Gate on the actual symbol this file needs instead, so a real
# hermes-agent install is required to collect real assertions here, and the
# leaked stub from a sibling test correctly still skips us.
pytest.importorskip(
    "hermes_cli.auth", reason="hermes-agent not installed in this checkout"
)
if not getattr(
    importlib.import_module("hermes_cli.auth"), "PROVIDER_REGISTRY", None
):
    pytest.skip(
        "hermes_cli.auth has no PROVIDER_REGISTRY — hermes-agent not installed "
        "in this checkout (only a minimal stub from an unrelated test)",
        allow_module_level=True,
    )


# (provider_id in hermes_cli.auth.PROVIDER_REGISTRY, the pip-importable module
# name for its lazily-gated SDK, the exact pin the Containerfile must carry —
# kept in lockstep with hermes-agent's own pyproject.toml [project.optional-dependencies]).
_SDK_GATED_PROVIDERS = [
    ("anthropic", "anthropic", "anthropic==0.87.0"),
    ("bedrock", "boto3", "boto3==1.42.89"),
    ("azure-foundry", "azure.identity", "azure-identity==1.25.3"),
]


class TestProviderRegistryHasTheseIds:
    """Sanity: the ids above are really in the catalogue Safent mirrors — if
    hermes-agent ever renames/drops one, this fails loudly instead of the
    SDK-import test below silently testing a dead id."""

    @pytest.mark.parametrize("provider_id,_mod,_pin", _SDK_GATED_PROVIDERS)
    def test_provider_id_is_registered(self, provider_id: str, _mod: str, _pin: str) -> None:
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert provider_id in PROVIDER_REGISTRY, (
            f"{provider_id!r} missing from hermes_cli PROVIDER_REGISTRY — "
            "update _SDK_GATED_PROVIDERS (and the Containerfile pins) to match."
        )


class TestProviderSdksAreImportable:
    """The actual regression guard: every provider the UI offers whose
    runtime path needs a third-party SDK must have that SDK importable in
    THIS image, not deferred to a first-use pip install that cannot work in
    the hardened container. This is what should have failed the container
    build instead of the user's chat (spec 025 hallazgo #2)."""

    @pytest.mark.parametrize("provider_id,module_name,pin", _SDK_GATED_PROVIDERS)
    def test_sdk_importable(self, provider_id: str, module_name: str, pin: str) -> None:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            pytest.fail(
                f"provider {provider_id!r} is offered by Safent's providers UI "
                f"but its SDK ({module_name!r}) is not installed — add {pin!r} "
                "to ops/container/Containerfile's pip install line "
                f"(hermes-agent's own pyproject.toml pins it exactly). "
                f"Original error: {exc}"
            )

    def test_anthropic_adapter_resolves_the_sdk(self) -> None:
        """End-to-end through hermes-agent's own lazy-import wrapper (not just
        `import anthropic` — this is the exact call chat makes)."""
        from agent.anthropic_adapter import _get_anthropic_sdk

        assert _get_anthropic_sdk() is not None, (
            "agent.anthropic_adapter._get_anthropic_sdk() returned None — "
            "the anthropic package is installed but the adapter still can't "
            "see it (check for a second/shadowed install)."
        )

    def test_bedrock_adapter_resolves_boto3(self) -> None:
        from agent.bedrock_adapter import has_aws_credentials

        # Only asserts the IMPORT path works (boto3 present) — it does NOT
        # assert real AWS credentials exist in this sandbox.
        has_aws_credentials()

    def test_azure_identity_adapter_resolves_the_sdk(self) -> None:
        from agent.azure_identity_adapter import has_azure_identity_installed

        assert has_azure_identity_installed() is True
