"""SDK wiring — the only place that knows how to build a Composio SDK handle.

Default (no `transport` injected): `composio.Composio(api_key=...)`, exactly
today's behaviour — its own httpx client, proxy env vars honoured, redirects
followed.

Injected `transport` (REQ-07, Enterprise's anti-SSRF boundary): the four
resource namespaces are built directly on top of `composio.client.HttpClient`
with an httpx.Client wrapping that transport, `trust_env=False` (never reads
proxy env vars once a transport is pinned) and `follow_redirects=False`. This
bypasses `composio.Composio.__init__`, which does not forward a custom
`http_client` — it is the same composition `composio.sdk.Composio` performs
internally, just assembled here so the transport can be pinned.

Telemetry side channel: every `composio.core.models.*` Resource method is
wrapped by `trace_method`, which — unless the `allow_tracking` contextvar is
False — fires a bare `httpx.post` (its OWN client, ignoring ours) from a
background thread on every call, and on failure attaches
`traceback.format_exc()` to the payload. `composio.Composio.__init__` sets
`allow_tracking` from its kwargs (default True); nothing else does. When a
transport is pinned we turn it off for the lifetime of this handle — the
whole point of pinning a transport is that NOTHING leaves through another
channel.

File uploads (`composio.core.models._files`, `requests.get` on arbitrary
URLs) are a separate potential egress path, but only reachable when
`Tools(..., dangerously_allow_auto_upload_download_files=True)` — we never
pass that, so it stays at its default `False` (asserted in
`test_default_behaviour_unchanged_when_no_transport_injected` and the
transport test).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from composio import Composio
from composio.client import HttpClient
from composio.core.models import AuthConfigs, ConnectedAccounts, Toolkits, Tools
from composio.core.models.base import allow_tracking
from composio.core.provider._openai import OpenAIProvider
from composio.utils.toolkit_version import get_toolkit_versions

_PROVIDER_NAME = "openai"
_DEFAULT_TIMEOUT_S = 30.0


class SdkHandle(Protocol):
    """The subset of `composio.Composio` this package actually calls.

    Read-only properties (not plain attributes): `composio.Composio` exposes
    these as mutable instance attributes, `_PinnedSdkHandle` as a frozen
    dataclass — the protocol only promises what this package needs, a read.
    """

    @property
    def toolkits(self) -> Toolkits: ...
    @property
    def connected_accounts(self) -> ConnectedAccounts: ...
    @property
    def auth_configs(self) -> AuthConfigs: ...
    @property
    def tools(self) -> Tools[Any, Any]: ...


@dataclass(frozen=True, slots=True)
class _PinnedSdkHandle:
    toolkits: Toolkits
    connected_accounts: ConnectedAccounts
    auth_configs: AuthConfigs
    tools: Tools[Any, Any]


def build_sdk(api_key: str, *, transport: httpx.BaseTransport | None) -> SdkHandle:
    if transport is None:
        return Composio(api_key=api_key)

    allow_tracking.set(False)
    http_client = HttpClient(
        provider=_PROVIDER_NAME,
        api_key=api_key,
        http_client=httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=_DEFAULT_TIMEOUT_S,
        ),
    )
    provider = OpenAIProvider()
    return _PinnedSdkHandle(
        toolkits=Toolkits(client=http_client),
        connected_accounts=ConnectedAccounts(client=http_client),
        auth_configs=AuthConfigs(client=http_client),
        tools=Tools(
            client=http_client,
            provider=provider,
            toolkit_versions=get_toolkit_versions(None),
        ),
    )
