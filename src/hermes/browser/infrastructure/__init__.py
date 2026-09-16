"""Infrastructure layer del browser: registries + agent-browser CLI.

The concrete LLM/replay drivers (PlaywrightDriver/StagehandDriver/CdpPlaywrightDriver)
were a parallel duplicate of hermes-agent's native browser; the agent browses via
the native tools and live teaching uses the CDP screencast live-view. Removed.

OpenShellSandboxProvider (egress sandbox del orquestador spec-002) se
archivo en `archive/browser-spec-002` — NO-GO fase 4, parcado. Ver
specs/025-safent-repaso/oleada-1.md §L1b.
"""

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
