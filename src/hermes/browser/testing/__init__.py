"""Helpers de testing del browser module.

    from hermes.browser.testing import FakeBrowserDriver, scripted_step
    from hermes.browser.testing import FakeMcpSession
    from hermes.browser.testing import FakeAgentBrowserCli

`FakeBrowserDriver` permite que las verticales testen su composicion sin
chromium real. Acepta `scripted_step(...)` para devolver outcomes deterministas.

`FakeMcpSession` permite testear `PlaywrightMcpDriver` sin Node/npx/browser.

`FakeAgentBrowserCli` permite testear `AgentBrowserDriver` sin el binario Rust.
"""

from hermes.browser.testing.fake_agent_browser_cli import FakeAgentBrowserCli
from hermes.browser.testing.fakes import (
    FakeBrowserDriver,
    ScriptedStep,
    scripted_step,
)

__all__ = [
    "FakeAgentBrowserCli",
    "FakeBrowserDriver",
    "ScriptedStep",
    "scripted_step",
]
