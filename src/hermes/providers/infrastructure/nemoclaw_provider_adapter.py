"""NemoClawProviderAdapter — maps ResolvedModel → NemoClaw route string.

NemoClaw routing uses the hermes_cli_slug from the catalog as the route
identifier. For CUSTOM_BASE_URL providers, the base_url is also passed
so NemoClaw can resolve the endpoint.

This adapter is a stub for future NemoClaw integration. The design
(spec 016) mandates its presence so the factory has a complete adapter set.
"""

from __future__ import annotations

from hermes.providers.domain.canonical import HermesCliRoute
from hermes.providers.domain.ports import ResolvedModel


def nemoclaw_route_from_resolved(resolved: ResolvedModel) -> str:
    """Return the NemoClaw route identifier for a ResolvedModel.

    For EXPLICIT_OPENAI_COMPAT providers, the route is "openrouter" (the
    aggregator path with explicit URL). For REGISTERED_SLUG, it is the
    hermes_cli_slug directly. For CUSTOM_BASE_URL, it is "custom".
    """
    canonical = resolved.canonical
    if canonical.route is HermesCliRoute.EXPLICIT_OPENAI_COMPAT:
        return "openrouter"
    if canonical.route is HermesCliRoute.CUSTOM_BASE_URL:
        return "custom"
    return canonical.hermes_cli_slug
