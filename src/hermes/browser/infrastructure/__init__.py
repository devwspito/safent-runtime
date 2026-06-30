"""Infrastructure layer del browser: registries + agent-browser CLI.

The concrete LLM/replay drivers (PlaywrightDriver/StagehandDriver/CdpPlaywrightDriver)
were a parallel duplicate of hermes-agent's native browser; the agent browses via
the native tools and live teaching uses the CDP screencast live-view. Removed.
"""

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
from hermes.browser.infrastructure.in_memory_selector_registry import (
    InMemorySelectorRegistry,
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
__all__ = [
    # agent-browser (optional, experimental)
    "AgentBrowserCli",
    "AgentBrowserCommandError",
    "AgentBrowserNotInstalledError",
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
    # selector registry
    "InMemorySelectorRegistry",
    "SelectorStore",
    "SelectorTamperedError",
    "SignedSelectorRegistry",
    "StoredSelector",
    "build_signed",
    "sign_selector",
    "verify_selector_signature",
]

# Re-export SelectorAuthor for convenience
from hermes.browser.domain.selector import SelectorAuthor  # noqa: E402

__all__ += ["SelectorAuthor"]
