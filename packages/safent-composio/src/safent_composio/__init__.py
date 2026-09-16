"""safent_composio — the ONE Composio client, shared by every product.

Never copied (spec 002 research.md, Decision 1): both safent-runtime
(Community) and safent-control-enterprise (Enterprise) import this package
instead of vendoring their own SDK wrapper. The contract each repo tests
against lives at `contracts/composio_port.pyi` in the Enterprise spec.
"""

from safent_composio.client import ComposioClient
from safent_composio.errors import ComposioApiError
from safent_composio.values import (
    AuthConfigInfo,
    ConnectedAccountInfo,
    ConnectionInitResult,
    ToolInfo,
    ToolkitInfo,
)

__all__ = [
    "AuthConfigInfo",
    "ComposioApiError",
    "ComposioClient",
    "ConnectedAccountInfo",
    "ConnectionInitResult",
    "ToolInfo",
    "ToolkitInfo",
]
