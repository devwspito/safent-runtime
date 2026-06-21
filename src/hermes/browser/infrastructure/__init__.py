"""Infrastructure layer del browser: drivers concretos + registries."""

from hermes.browser.infrastructure.cdp_playwright_driver import (
    CdpPlaywrightDriver,
    build_driver_from_env,
)
from hermes.browser.infrastructure.openshell_sandbox_provider import (
    EgressAllowEntry,
    OpenShellGatewayNotRunningError,
    OpenShellPolicyPushError,
    OpenShellSandboxProvisionError,
    OpenShellSandboxProvider,
    OpenShellTeardownError,
    SandboxProvisionResult,
    build_egress_policy_yaml,
    make_egress_approved_sites_provider,
)
from hermes.browser.infrastructure.agent_browser_cli import (
    AgentBrowserCli,
    AgentBrowserCommandError,
    AgentBrowserNotInstalledError,
)
from hermes.browser.infrastructure.agent_browser_driver import (
    AgentBrowserDriver,
)
from hermes.browser.infrastructure.in_memory_selector_registry import (
    InMemorySelectorRegistry,
)
from hermes.browser.infrastructure.mcp_session import (
    McpNotInstalledError,
    McpServerConnectionError,
    StdioMcpSession,
)
from hermes.browser.infrastructure.playwright_driver import (
    PlaywrightDriver,
    PlaywrightNotInstalledError,
)
from hermes.browser.infrastructure.playwright_mcp_driver import (
    PlaywrightMcpDriver,
)
from hermes.browser.infrastructure.signed_selector_registry import (
    SelectorStore,
    SelectorTamperedError,
    SignedSelectorRegistry,
    StoredSelector,
    build_signed,
    sign_selector,
    verify_selector_signature,
)
from hermes.browser.infrastructure.stagehand_driver import (
    StagehandDriver,
    StagehandNotInstalledError,
)

__all__ = [
    # agent-browser (optional, experimental)
    "AgentBrowserCli",
    "AgentBrowserCommandError",
    "AgentBrowserDriver",
    "AgentBrowserNotInstalledError",
    # CDP sandbox driver (OpenShell integration)
    "CdpPlaywrightDriver",
    "build_driver_from_env",
    # OpenShell sandbox provider
    "EgressAllowEntry",
    "OpenShellGatewayNotRunningError",
    "OpenShellPolicyPushError",
    "OpenShellSandboxProvisionError",
    "OpenShellSandboxProvider",
    "OpenShellTeardownError",
    "SandboxProvisionResult",
    "build_egress_policy_yaml",
    "make_egress_approved_sites_provider",
    # existing drivers
    "InMemorySelectorRegistry",
    "McpNotInstalledError",
    "McpServerConnectionError",
    "PlaywrightDriver",
    "PlaywrightMcpDriver",
    "PlaywrightNotInstalledError",
    "SelectorStore",
    "SelectorTamperedError",
    "SignedSelectorRegistry",
    "StagehandDriver",
    "StagehandNotInstalledError",
    "StdioMcpSession",
    "StoredSelector",
    "build_signed",
    "sign_selector",
    "verify_selector_signature",
]

# Re-export SelectorAuthor for convenience
from hermes.browser.domain.selector import SelectorAuthor  # noqa: E402

__all__ += ["SelectorAuthor"]
