"""ProviderCatalog — THE authoritative table: ProviderKind → CanonicalProvider.

Replaces two stale, divergent tables:
  - LITELLM_PREFIX in shell_server/providers/domain.py
  - _HERMES_SLUG_BY_PREFIX in runtime/nous_engine.py

A single entry here drives ALL three engines: litellm, nous (hermes-agent),
and NemoClaw. If a ProviderKind is missing from this table, that is a compile-
time error caught by test_canonical_catalog_completeness.

hermes_cli slug verification (as of hermes-agent 0.15.1):
  REGISTERED_SLUG entries verified against hermes_cli/auth.py::PROVIDER_REGISTRY
  and hermes_cli/providers.py::HERMES_OVERLAYS + ALIASES.
  "openrouter" is not in PROVIDER_REGISTRY (by design, it is the fallback
  aggregator) but is always valid — its path is handled before the registry
  lookup in resolve_provider().

OPENAI fix (bug recurring 4 times):
  The old code mapped OPENAI → requested="openai-api" which does NOT exist
  in PROVIDER_REGISTRY → AuthError("Unknown provider 'openai-api'").
  Correct mapping: requested="openrouter" + explicit_base_url=api.openai.com
  (hermes_cli treats OpenAI as an openrouter-compat endpoint with custom URL).
  Alternatively, passing explicit_api_key causes resolve_provider("openai") to
  return "openrouter" via the alias path. We use EXPLICIT_OPENAI_COMPAT so
  the base_url is always set explicitly and never relies on env aliasing.
"""

from __future__ import annotations

from hermes.providers.domain.canonical import CanonicalProvider, HermesCliRoute
from hermes.providers.domain.errors import UnmappedProviderError
from hermes.shell_server.providers.domain import ProviderKind

# ---------------------------------------------------------------------------
# Route shorthands
# ---------------------------------------------------------------------------
_REG = HermesCliRoute.REGISTERED_SLUG
_EXP = HermesCliRoute.EXPLICIT_OPENAI_COMPAT
_CUS = HermesCliRoute.CUSTOM_BASE_URL

# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
_CATALOG: dict[ProviderKind, CanonicalProvider] = {
    # ── Cloud first-party ──────────────────────────────────────────────────
    ProviderKind.ANTHROPIC: CanonicalProvider(
        litellm_prefix="anthropic",
        hermes_cli_slug="anthropic",
        route=_REG,
    ),
    # OPENAI: the slug to pass is "openrouter" (the aggregator path) with an
    # explicit base_url pointing to api.openai.com. This is how hermes_cli
    # handles direct-OpenAI credentials. Passing "openai" or "openai-api"
    # both fail (alias → openrouter but without the explicit URL, or unknown).
    ProviderKind.OPENAI: CanonicalProvider(
        litellm_prefix="openai",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://api.openai.com/v1",
    ),
    ProviderKind.AZURE_OPENAI: CanonicalProvider(
        litellm_prefix="azure",
        hermes_cli_slug="azure-foundry",
        route=_CUS,
        requires_base_url=True,
    ),
    ProviderKind.GEMINI: CanonicalProvider(
        litellm_prefix="gemini",
        hermes_cli_slug="gemini",
        route=_REG,
    ),
    ProviderKind.BEDROCK: CanonicalProvider(
        litellm_prefix="bedrock",
        hermes_cli_slug="bedrock",
        route=_REG,
    ),
    # ── Chinese cloud ──────────────────────────────────────────────────────
    ProviderKind.DEEPSEEK: CanonicalProvider(
        litellm_prefix="deepseek",
        hermes_cli_slug="deepseek",
        route=_REG,
    ),
    ProviderKind.MOONSHOT: CanonicalProvider(
        litellm_prefix="moonshot",
        hermes_cli_slug="kimi-for-coding",
        route=_REG,
    ),
    ProviderKind.ZHIPU: CanonicalProvider(
        litellm_prefix="zhipu",
        hermes_cli_slug="zai",
        route=_REG,
    ),
    ProviderKind.DOUBAO: CanonicalProvider(
        litellm_prefix="volcengine",
        # Doubao (ByteDance) is OpenAI-compatible; routed through openrouter
        # path with the explicit Doubao endpoint.
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
    ProviderKind.QWEN_DASHSCOPE: CanonicalProvider(
        litellm_prefix="dashscope",
        hermes_cli_slug="alibaba",
        route=_REG,
    ),
    # ── Proxies / aggregators ──────────────────────────────────────────────
    ProviderKind.OPENROUTER: CanonicalProvider(
        litellm_prefix="openrouter",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://openrouter.ai/api/v1",
    ),
    ProviderKind.MISTRAL: CanonicalProvider(
        litellm_prefix="mistral",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://api.mistral.ai/v1",
    ),
    ProviderKind.COHERE: CanonicalProvider(
        litellm_prefix="cohere",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://api.cohere.com/v1",
    ),
    ProviderKind.GROQ: CanonicalProvider(
        litellm_prefix="groq",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://api.groq.com/openai/v1",
    ),
    ProviderKind.TOGETHER: CanonicalProvider(
        litellm_prefix="together_ai",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://api.together.xyz/v1",
    ),
    ProviderKind.FIREWORKS: CanonicalProvider(
        litellm_prefix="fireworks_ai",
        hermes_cli_slug="openrouter",
        route=_EXP,
        default_base_url="https://api.fireworks.ai/inference/v1",
    ),
    # ── Self-hosted ────────────────────────────────────────────────────────
    ProviderKind.VLLM: CanonicalProvider(
        litellm_prefix="hosted_vllm",
        hermes_cli_slug="custom",
        route=_CUS,
        requires_base_url=True,
    ),
    ProviderKind.OLLAMA: CanonicalProvider(
        litellm_prefix="ollama",
        hermes_cli_slug="custom",
        route=_CUS,
        requires_base_url=True,
        default_base_url="http://localhost:11434",
    ),
    ProviderKind.LM_STUDIO: CanonicalProvider(
        litellm_prefix="openai",
        hermes_cli_slug="lmstudio",
        route=_REG,
        requires_base_url=True,
        default_base_url="http://localhost:1234/v1",
    ),
    ProviderKind.LLAMA_CPP: CanonicalProvider(
        litellm_prefix="openai",
        hermes_cli_slug="custom",
        route=_CUS,
        requires_base_url=True,
        default_base_url="http://localhost:8080/v1",
    ),
    ProviderKind.TGI: CanonicalProvider(
        litellm_prefix="huggingface",
        hermes_cli_slug="huggingface",
        route=_REG,
    ),
    ProviderKind.OPENAI_COMPATIBLE: CanonicalProvider(
        litellm_prefix="openai",
        hermes_cli_slug="custom",
        route=_CUS,
        requires_base_url=True,
    ),
    # ── Suscripciones OAuth (sin clave API) ────────────────────────────────
    # Nous Portal: device-code flow; las credenciales viven en el auth-store de
    # hermes_cli (HERMES_HOME). REGISTERED_SLUG con explicit_api_key=None →
    # resolve_runtime_provider("nous") lee el OAuth store, igual que
    # `hermes --provider nous` tras `hermes auth add nous`.
    ProviderKind.NOUS: CanonicalProvider(
        litellm_prefix="nous",
        hermes_cli_slug="nous",
        route=_REG,
    ),
}

# Verify at module load time that every ProviderKind has an entry.
# This is a sanity check that also runs during pytest collection.
_missing = [k for k in ProviderKind if k not in _CATALOG]
if _missing:
    raise UnmappedProviderError(
        f"ProviderCatalog is incomplete — missing entries for: {_missing}. "
        "Add a CanonicalProvider entry for each new ProviderKind."
    )


def canonical_for(kind: ProviderKind) -> CanonicalProvider:
    """Return the CanonicalProvider for a ProviderKind.

    Always succeeds — the module-level check above guarantees completeness.
    """
    return _CATALOG[kind]


def build_litellm_model_string(kind: ProviderKind, model: str) -> str:
    """Build the litellm 'prefix/model' string for a given kind and model name.

    Replaces LITELLM_PREFIX dict + litellm_model_string() in domain.py.
    Idempotent: if model already starts with the prefix, returns as-is.
    """
    prefix = _CATALOG[kind].litellm_prefix
    if model.startswith(f"{prefix}/"):
        return model
    return f"{prefix}/{model}"
