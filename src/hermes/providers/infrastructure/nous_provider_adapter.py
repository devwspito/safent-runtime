"""NousProviderAdapter — maps ResolvedModel → HermesCliRequest for hermes-agent.

Also exposes nous_request_from_model_config() for the fallback env/headless path
where only a ModelConfig (not a full ResolvedModel) is available. This replaces
the old _HERMES_SLUG_BY_PREFIX dict in nous_engine.py.

This adapter is the ONLY place that translates ResolvedModel into the kwargs
expected by resolve_runtime_provider() in hermes_cli.runtime_provider.

Routing logic (mirrors hermes_cli semantics, verified against 0.15.1):

  REGISTERED_SLUG:
    Pass requested=hermes_cli_slug with explicit_api_key=key.
    hermes_cli looks up the provider in PROVIDER_REGISTRY directly.
    Example: anthropic, gemini, bedrock, deepseek, zai, kimi-for-coding.

  EXPLICIT_OPENAI_COMPAT:
    Pass requested="openrouter" with explicit_base_url=canonical.default_base_url
    (or provider.base_url if configured) and explicit_api_key=key.
    hermes_cli routes through _resolve_openrouter_runtime with the explicit URL.
    The "openrouter" slug bypasses the ALIASES lookup and goes directly to the
    aggregator path. Example: OPENAI → openrouter + api.openai.com/v1.

  CUSTOM_BASE_URL:
    Pass requested="custom" with explicit_base_url=provider.base_url.
    hermes_cli routes through _resolve_named_custom_runtime or the openrouter
    path with explicit URL. Example: vLLM, Ollama, llama.cpp, Azure Foundry.
    Azure uses requested="azure-foundry" (REGISTERED_SLUG-like with special handling).
"""

from __future__ import annotations

from hermes.providers.domain.canonical import CanonicalProvider, HermesCliRoute
from hermes.providers.domain.ports import HermesCliRequest, ResolvedModel


def nous_request_from_resolved(
    resolved: ResolvedModel,
    *,
    target_model: str | None = None,
) -> HermesCliRequest:
    """Produce a HermesCliRequest from a ResolvedModel.

    target_model: the bare model name (without prefix) to pass as target_model
    kwarg to resolve_runtime_provider(). If None, hermes_cli uses its own
    config.yaml default.
    """
    canonical = resolved.canonical
    route = canonical.route

    if route is HermesCliRoute.REGISTERED_SLUG:
        return HermesCliRequest(
            requested=canonical.hermes_cli_slug,
            explicit_api_key=resolved.api_key,
            explicit_base_url=resolved.base_url,
            target_model=target_model,
        )

    if route is HermesCliRoute.EXPLICIT_OPENAI_COMPAT:
        return HermesCliRequest(
            requested="openrouter",
            explicit_api_key=resolved.api_key,
            explicit_base_url=resolved.base_url or canonical.default_base_url,
            target_model=target_model,
        )

    # CUSTOM_BASE_URL — Azure Foundry has its own registered slug; all others use "custom".
    requested = (
        canonical.hermes_cli_slug
        if canonical.hermes_cli_slug not in {"custom"}
        else "custom"
    )
    return HermesCliRequest(
        requested=requested,
        explicit_api_key=resolved.api_key,
        explicit_base_url=resolved.base_url or canonical.default_base_url,
        target_model=target_model,
    )


def nous_request_from_model_config(
    model_config: "ModelConfig",  # type: ignore[name-defined]  # noqa: F821
) -> "tuple[HermesCliRequest, str]":
    """Map a ModelConfig to (HermesCliRequest, bare_model) using the catalog.

    Fallback path for env/headless mode where only a ModelConfig (derived from
    HERMES_MODEL env var or env fallback) is available — no vault/ResolvedModel.

    Replaces the old _HERMES_SLUG_BY_PREFIX dict that had 'openai' → 'openai-api'
    (broken slug). This function builds a reverse-map from litellm prefix →
    CanonicalProvider using the authoritative catalog, and applies the same
    routing logic as nous_request_from_resolved().

    Returns (HermesCliRequest, bare_model) where bare_model is the model name
    without the litellm prefix (e.g. 'gpt-4o' from 'openai/gpt-4o').
    """
    raw = (model_config.model or "").strip()
    if "/" in raw:
        prefix, bare = raw.split("/", 1)
    else:
        prefix, bare = "", raw

    canonical = _canonical_from_litellm_prefix(prefix.strip().lower())

    if canonical is None:
        # Unknown prefix — pass through as-is (custom or new provider).
        # Let hermes_cli resolve or fail with a clear error.
        return HermesCliRequest(
            requested=prefix.strip().lower() or None,  # type: ignore[arg-type]
            explicit_api_key=model_config.api_key or None,
            explicit_base_url=model_config.base_url or None,
            target_model=bare or None,
        ), bare

    # Apply route logic as in nous_request_from_resolved().
    route = canonical.route
    explicit_base_url = model_config.base_url or canonical.default_base_url

    if route is HermesCliRoute.REGISTERED_SLUG:
        return HermesCliRequest(
            requested=canonical.hermes_cli_slug,
            explicit_api_key=model_config.api_key or None,
            explicit_base_url=model_config.base_url or None,
            target_model=bare or None,
        ), bare

    if route is HermesCliRoute.EXPLICIT_OPENAI_COMPAT:
        return HermesCliRequest(
            requested="openrouter",
            explicit_api_key=model_config.api_key or None,
            explicit_base_url=explicit_base_url,
            target_model=bare or None,
        ), bare

    # CUSTOM_BASE_URL
    requested = (
        canonical.hermes_cli_slug
        if canonical.hermes_cli_slug not in {"custom"}
        else "custom"
    )
    return HermesCliRequest(
        requested=requested,
        explicit_api_key=model_config.api_key or None,
        explicit_base_url=explicit_base_url,
        target_model=bare or None,
    ), bare


def _canonical_from_litellm_prefix(prefix: str) -> CanonicalProvider | None:
    """Reverse-map a litellm prefix to a CanonicalProvider, or None if unknown.

    Uses the authoritative catalog. When multiple ProviderKinds share a prefix
    (e.g. 'openai' is used by OPENAI, LM_STUDIO, LLAMA_CPP, OPENAI_COMPATIBLE),
    OPENAI takes precedence (it is the most common cloud provider for that prefix).
    """
    from hermes.providers.domain.catalog import _CATALOG  # noqa: PLC0415
    from hermes.shell_server.providers.domain import ProviderKind  # noqa: PLC0415

    # Priority order for ambiguous prefixes
    _PRIORITY = [
        ProviderKind.OPENAI,
        ProviderKind.ANTHROPIC,
        ProviderKind.GEMINI,
        ProviderKind.AZURE_OPENAI,
    ]

    for kind in _PRIORITY:
        entry = _CATALOG.get(kind)
        if entry and entry.litellm_prefix == prefix:
            return entry

    for kind, entry in _CATALOG.items():
        if entry.litellm_prefix == prefix:
            return entry

    return None
