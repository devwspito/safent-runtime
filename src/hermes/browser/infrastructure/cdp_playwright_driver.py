"""CdpPlaywrightDriver: PlaywrightDriver variant that connects to an EXISTING browser via CDP.

Activates when HERMES_BROWSER_SANDBOX=openshell (or when `cdp_url` is injected
directly). Instead of launching a local Chromium process, it calls
`playwright.chromium.connect_over_cdp(cdp_url)` to attach to the headless Chromium
already running inside an OpenShell sandbox.

The browser tools (capability_tool_specs.py, BrowserPort) work unchanged — only the
browser connection origin changes. Local Playwright launch remains the default path.

Design:
  - Subclasses PlaywrightDriver to inherit the full execute/dispatch surface.
  - Overrides `start()` only — connection replaces launch.
  - `close()` disconnects from the remote browser without stopping it (the sandbox
    owns the process lifecycle; the provider handles teardown separately).
  - `ignore_https_errors=True` by default because the OpenShell network proxy may
    present its own TLS cert for passthrough (SNI-only mode avoids MITM entirely,
    but we keep the flag for flexibility).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from hermes.browser.infrastructure.playwright_driver import (
    PlaywrightDriver,
    PlaywrightNotInstalledError,
)

logger = logging.getLogger(__name__)

_ENV_SANDBOX: str = "HERMES_BROWSER_SANDBOX"
_ENV_CDP_URL: str = "HERMES_CDP_URL"
_DEFAULT_CDP_URL: str = "http://127.0.0.1:9222"


class CdpPlaywrightDriver(PlaywrightDriver):
    """PlaywrightDriver that attaches to an existing Chromium via CDP.

    Construction:
        driver = CdpPlaywrightDriver(cdp_url="http://127.0.0.1:9222")
        await driver.start()
        # ... use like any BrowserPort
        await driver.close()

    The driver does NOT stop or delete the remote Chromium on close(); the
    OpenShellSandboxProvider.teardown() is responsible for that.
    """

    def __init__(
        self,
        *,
        cdp_url: str = _DEFAULT_CDP_URL,
        timeout_ms: int = 30_000,
    ) -> None:
        # Initialise parent with headless=True (no local launch) and the timeout.
        # cert_pem_path / user_agent not relevant for a remote browser connection.
        super().__init__(
            headless=True,
            timeout_ms=timeout_ms,
            ignore_https_errors=True,
        )
        self._cdp_url = cdp_url
        # Playwright objects — overridden in start() vs parent's launch path
        self._pw_instance: Any = None

    @classmethod
    def from_env(cls) -> "CdpPlaywrightDriver":
        """Build from environment variables.

        HERMES_CDP_URL   — full CDP endpoint (default: http://127.0.0.1:9222)
        """
        cdp_url = os.environ.get(_ENV_CDP_URL, _DEFAULT_CDP_URL)
        return cls(cdp_url=cdp_url)

    @property
    def driver_name(self) -> str:
        return "cdp_playwright"

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "cdp_url": self._cdp_url,
            "mode": "connect_over_cdp",
            "sandbox": "openshell",
            "supports_action_caching": False,
            "supports_vision": False,
        }

    async def start(self) -> None:
        """Connect to the existing Chromium via CDP instead of launching one."""
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise PlaywrightNotInstalledError(
                "Playwright is not installed. Install with:\n"
                "    pip install 'hermes-runtime[browser]'\n"
                "    playwright install chromium"
            ) from exc

        self._pw_instance = await async_playwright().start()
        self._playwright = self._pw_instance

        logger.info(
            "hermes.browser.cdp_driver.connecting",
            extra={"cdp_url": self._cdp_url},
        )
        self._browser = await self._pw_instance.chromium.connect_over_cdp(
            self._cdp_url,
            timeout=float(self._timeout_ms),
        )

        # Always open a FRESH context + page so we start from a known blank state.
        # Reusing an existing context risks stale cookies, cached error pages, or
        # pages that were left in a broken state from a previous session.
        # The remote Chromium process is not stopped by our close(); other contexts
        # opened by other actors (e.g. the /tmp harness) remain untouched.
        self._context = await self._browser.new_context(
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()

        logger.info(
            "hermes.browser.cdp_driver.connected",
            extra={"cdp_url": self._cdp_url, "url": self._page.url},
        )

    async def close(self) -> None:
        """Disconnect from the remote browser. Does NOT stop the remote Chromium process."""
        if self._closed:
            return
        self._closed = True
        try:
            # Only close the context we opened; do not close the remote browser itself.
            if self._context is not None:
                await self._context.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.cdp_driver.close_failed",
                extra={"cdp_url": self._cdp_url, "error": str(exc)},
            )
        logger.info(
            "hermes.browser.cdp_driver.disconnected",
            extra={"cdp_url": self._cdp_url},
        )


def build_driver_from_env() -> CdpPlaywrightDriver | PlaywrightDriver:
    """Factory: returns a CdpPlaywrightDriver if HERMES_BROWSER_SANDBOX=openshell,
    otherwise a plain PlaywrightDriver (local launch, existing default).

    Called from application/composition-root code; never from domain.
    """
    sandbox_mode = os.environ.get(_ENV_SANDBOX, "").lower()
    if sandbox_mode == "openshell":
        driver = CdpPlaywrightDriver.from_env()
        logger.info(
            "hermes.browser.driver_factory.openshell_cdp_selected",
            extra={"cdp_url": driver._cdp_url},
        )
        return driver

    return PlaywrightDriver()
