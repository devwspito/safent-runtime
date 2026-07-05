"""Mapping ProviderKind (Safent store) → hermes_cli PROVIDER_REGISTRY id.

This module is the single source of truth for translating a Safent ProviderKind
into the native provider_id that hermes_cli expects in config.yaml + .env.
All functions are pure and have no side effects.

Design rules:
- No hermes_cli import at module level: the baked image is required at runtime,
  but unit tests and CI must be able to import this module without hermes_cli.
- Fail-soft: unknown / un-mappable kinds fall back to ``openai-api`` (generic
  OpenAI-compatible endpoint) so the caller can still write a base_url + key.
- The module is intentionally thin — it encodes ONLY the mapping table; the
  actual .env / config.yaml writes stay in dbus_runtime_service._sync_to_native.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.shell_server.providers.domain import ProviderKind


@dataclass(frozen=True, slots=True)
class NativeProviderTarget:
    """Result of resolving a ProviderKind to a native hermes_cli provider."""

    provider_id: str
    """The key in hermes_cli.auth.PROVIDER_REGISTRY (e.g. ``openai-api``)."""

    env_var: str
    """Primary env-var that holds the API key (e.g. ``OPENAI_API_KEY``)."""

    base_url_env_var: str
    """Env-var for the base URL override, or ``""`` if provider has no URL override."""

    needs_base_url: bool
    """True when the caller must supply a base_url to complete the config.

    This is True for every OpenAI-compatible self-hosted or aggregator kind
    where the user's base_url is the only pointer to the actual endpoint.
    """


# ---------------------------------------------------------------------------
# Static mapping table: ProviderKind → (native_id, env_var, base_url_env_var)
#
# Rationale per entry:
#   - Kinds with a dedicated PROVIDER_REGISTRY entry use it directly: the
#     native resolver already knows their endpoint and env var.
#   - OpenAI-compatible providers without a dedicated registry entry map to
#     ``openai-api`` (OPENAI_API_KEY + OPENAI_BASE_URL).  The caller sets
#     base_url so the resolver reaches the correct endpoint.
#   - NOUS maps to ``nous`` (OAuth, no api_key path — env_var is intentionally
#     left empty so the caller skips the key-write branch).
# ---------------------------------------------------------------------------

_KIND_MAP: dict[ProviderKind, tuple[str, str, str, bool]] = {
    # (provider_id, env_var, base_url_env_var, needs_base_url)
    #
    # Direct native registry entries — provider_id exists in PROVIDER_REGISTRY.
    ProviderKind.OPENAI:           ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    False),
    ProviderKind.ANTHROPIC:        ("anthropic",    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", False),
    ProviderKind.GEMINI:           ("gemini",       "GOOGLE_API_KEY",    "GEMINI_BASE_URL",    False),
    ProviderKind.DEEPSEEK:         ("deepseek",     "DEEPSEEK_API_KEY",  "DEEPSEEK_BASE_URL",  False),
    ProviderKind.LM_STUDIO:        ("lmstudio",     "LM_API_KEY",        "LM_BASE_URL",        True),
    ProviderKind.OLLAMA:           ("ollama-cloud", "OLLAMA_API_KEY",    "OLLAMA_BASE_URL",    True),
    # Qwen Dashscope → alibaba in registry (DASHSCOPE_API_KEY)
    ProviderKind.QWEN_DASHSCOPE:   ("alibaba",      "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", False),
    # Moonshot → kimi-coding in registry
    ProviderKind.MOONSHOT:         ("kimi-coding",  "KIMI_API_KEY",      "KIMI_BASE_URL",      False),
    # NOUS: OAuth path — env_var intentionally empty (caller skips key-write)
    ProviderKind.NOUS:             ("nous",         "",                  "",                   False),
    #
    # OpenAI-compatible kinds without a dedicated PROVIDER_REGISTRY entry:
    # map to openai-api + base_url (the user's base_url is the endpoint pointer).
    ProviderKind.GROQ:             ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.MISTRAL:          ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.OPENROUTER:       ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.TOGETHER:         ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.FIREWORKS:        ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.COHERE:           ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.ZHIPU:            ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.DOUBAO:           ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.AZURE_OPENAI:     ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.BEDROCK:          ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.VLLM:             ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.LLAMA_CPP:        ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.TGI:              ("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
    ProviderKind.OPENAI_COMPATIBLE:("openai-api",   "OPENAI_API_KEY",    "OPENAI_BASE_URL",    True),
}


def kind_to_native_target(kind: ProviderKind) -> NativeProviderTarget:
    """Return the native hermes_cli target for ``kind``.

    Always succeeds: unknown kinds fall back to ``openai-api`` with
    ``needs_base_url=True`` so the caller can still forward the user's key.
    """
    entry = _KIND_MAP.get(kind)
    if entry is None:
        # Unmapped kind: treat as generic OpenAI-compatible.
        return NativeProviderTarget(
            provider_id="openai-api",
            env_var="OPENAI_API_KEY",
            base_url_env_var="OPENAI_BASE_URL",
            needs_base_url=True,
        )
    pid, env_var, base_url_env_var, needs_base_url = entry
    return NativeProviderTarget(
        provider_id=pid,
        env_var=env_var,
        base_url_env_var=base_url_env_var,
        needs_base_url=needs_base_url,
    )
