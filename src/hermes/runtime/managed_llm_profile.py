"""Pure preparation of a single-identity Hermes profile; does not activate it.

Never merge a personal config or environment. Callers supply the native
DEFAULT_CONFIG only to enumerate auxiliary tasks for the pinned Hermes build.
The gate in managed_llm remains closed until lifecycle and real error-route
verification are complete. No upstream monkeypatch or alternate inference SDK.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from hermes.runtime.model_config import ModelConfig


def build_profile(binding: ModelConfig, native_defaults: Mapping) -> dict:
    """Return secret-free native configuration with every known aux route pinned.

    The instance-scoped key is supplied only to the native main runtime from
    the vault, not persisted in config.yaml, exported as provider env or copied
    to MCP. Hermes's live runtime context must supply auxiliary authentication;
    the real-image matrix verifies that behavior before any activation.
    """
    endpoint = urlparse(binding.base_url or '')
    if (not binding.managed or binding.native_provider != 'custom'
            or not binding.model.startswith('custom/') or not binding.model[7:].strip()
            or not binding.api_key or endpoint.scheme != 'https' or not endpoint.hostname
            or endpoint.username or endpoint.query or endpoint.fragment):
        raise ValueError('A verified instance gateway binding is required')
    model = binding.model[7:]
    auxiliary = native_defaults.get('auxiliary', {})
    if not isinstance(auxiliary, Mapping) or not auxiliary:
        raise ValueError('Pinned Hermes auxiliary task catalog is required')
    routes = {name: {
        'provider': 'custom', 'model': model, 'base_url': binding.base_url,
        'api_key': '', 'api_mode': 'chat_completions', 'fallback_chain': [],
    } for name, value in auxiliary.items() if isinstance(name, str) and isinstance(value, Mapping)}
    if not routes:
        raise ValueError('Pinned Hermes auxiliary task catalog is empty')
    return {
        'model': {'provider': 'custom', 'default': model, 'base_url': binding.base_url,
                  'api_mode': 'chat_completions'},
        'auxiliary': {**routes, 'transient_retries': 0},
        'fallback_providers': [], 'fallback_model': None, 'custom_providers': [],
        # Do not copy personal tools/plugins/profile hooks with arbitrary defaults.
        # Managed tool manifests must be projected separately through the jaula.
        'mcp_servers': {}, 'plugins': {},
    }


def allowed_environment(source: Mapping[str, str], profile_home: Path) -> dict[str, str]:
    """Minimal inference profile environment, NOT a full daemon launch contract.

    No inherited provider credentials, proxies, SDK profile selectors, Python
    paths or loader settings. Infrastructure env needed by the existing daemon
    must be explicitly projected by the later lifecycle implementation.
    """
    if not profile_home.is_absolute():
        raise ValueError('Corporate profile home must be absolute')
    path = str(profile_home)
    return {
        'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': path, 'HERMES_HOME': path,
        'XDG_CONFIG_HOME': str(profile_home / '.config'),
        'XDG_CACHE_HOME': str(profile_home / '.cache'),
        'XDG_DATA_HOME': str(profile_home / '.local/share'),
        **{name: source[name] for name in ('LANG', 'LC_ALL', 'TZ') if source.get(name)},
    }
