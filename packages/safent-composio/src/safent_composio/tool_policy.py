"""Ads operations belong to their governed module, never generic Composio tools."""

ADS_MODULE_REQUIRED = "safent_ads_module_required"
ADS_MODULE_MESSAGE = "Usa Anuncios para que se apliquen cuentas, presupuestos y aprobaciones."


def is_ads_toolkit(slug: object) -> bool:
    return isinstance(slug, str) and slug.strip().lower() in {"googleads", "metaads"}


def requires_ads_module(tool_slug: object) -> bool:
    return isinstance(tool_slug, str) and is_ads_toolkit(tool_slug.strip().split("_", 1)[0])
