"""Value objects — the only shapes that cross the Composio SDK boundary.

No SDK object (composio / composio_client) ever leaves this package. Every
public method returns one of these immutable dataclasses instead.

Field sets are a SUPERSET of the Enterprise port contract
(contracts/composio_port.pyi): `auth_schemes` (ToolkitInfo) and `status`
(AuthConfigInfo) are shipped Community-only fields (Ads companion, v0.9.36)
that predate/exceed what Enterprise's port needs. `test_composio_port_contract.py`
only requires the contract's fields to be PRESENT, not that these are the
only ones — see that file's docstring.

`kw_only=True` on the four contract value objects: construction is keyword-only, so
adding or reordering a field can never silently mis-bind a positional caller in either
product (`ToolInfo` is runtime-only and keeps positional construction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolkitInfo:
    """Catalog entry for a Composio toolkit (app).

    `oauth_simple` / `managed_auth_available` are the SHARED classification
    (spec 002 research.md, Decision 1): if it diverges between products, the
    same organisation key would show a different catalog in each.
    """

    slug: str
    name: str
    description: str
    auth_schemes: tuple[str, ...] = ()
    oauth_simple: bool = True
    managed_auth_available: bool | None = None  # None == the provider doesn't declare it
    setup_required: bool = False


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """A single action exposed by a Composio toolkit."""

    slug: str
    description: str
    input_parameters: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectedAccountInfo:
    """A user-connected account on Composio cloud."""

    id: str
    toolkit_slug: str
    entity_id: str
    status: str
    auth_config_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionInitResult:
    """Result of initiating an OAuth connection."""

    connected_account_id: str
    redirect_url: str
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthConfigInfo:
    """A validated Composio auth config for one toolkit.

    `status` is Ads-companion-only (not in the Enterprise contract's 2-field
    `AuthConfigInfo`) — always "ENABLED" today (`_validate_auth_config`
    rejects anything else before constructing one).
    """

    id: str
    toolkit_slug: str
    status: str
