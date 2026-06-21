"""PlaywrightDriver: driver minimal sobre Playwright sin capa LLM.

Util para:
  - Tier 4 (replay): ejecutar scripts deterministas generados a partir de
    un run exitoso del LLM (NO usa LLM en runtime).
  - Tests E2E reproducibles sin pagar tokens LLM.
  - Modo "robot puro" donde el flujo esta pre-mapeado.

NO razona — el caller pasa instrucciones primitivas (`click_selector`,
`fill_selector`, `goto`). Para razonar, usar StagehandDriver / BrowserUseDriver.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from hermes.browser.domain.step import Step, StepKind, StepOutcome

logger = logging.getLogger(__name__)


class PlaywrightNotInstalledError(RuntimeError):
    """Paquete `playwright` no esta instalado. Instala `hermes-runtime[browser]`."""


class PlaywrightDriver:
    """Driver minimal sobre Playwright.

    Construccion:
        driver = PlaywrightDriver(
            headless=True,
            user_agent="Mozilla/5.0 ...",
            cert_pem_path=None,            # opcional: cert client TLS PEM
            cert_pem_password=None,
        )
        await driver.start()

    Soporta steps:
      - NAVIGATE: payload = {"url": str}
      - ACT con `payload.click_selector`: click(selector).
      - ACT con `payload.fill_selector` + `fill_value`: fill(selector, value).
      - ACT con `payload.press_key`: keyboard.press(key).
      - EXTRACT con `payload.selector` + `attr` ("text" | "value" | "href" | ...).
      - SCREENSHOT.
      - WAIT con `payload.state` ("networkidle" | "load" | "domcontentloaded").
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        user_agent: str | None = None,
        viewport: tuple[int, int] = (1280, 720),
        timeout_ms: int = 30_000,
        cert_pem_path: str | None = None,
        cert_pem_password: str | None = None,
        ignore_https_errors: bool = False,
    ) -> None:
        self._headless = headless
        self._user_agent = user_agent
        self._viewport = viewport
        self._timeout_ms = timeout_ms
        self._cert_pem_path = cert_pem_path
        self._cert_pem_password = cert_pem_password
        self._ignore_https_errors = ignore_https_errors
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._closed = False

    @property
    def driver_name(self) -> str:
        return "playwright"

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "playwright_headless": self._headless,
            "supports_action_caching": False,
            "supports_vision": False,
            "supports_cert_client": self._cert_pem_path is not None,
        }

    @property
    def page(self) -> Any:
        """Acceso al `Page` Playwright crudo (para integraciones avanzadas)."""
        return self._page

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise PlaywrightNotInstalledError(
                "Playwright no esta instalado. Instala con:\n"
                "    pip install 'hermes-runtime[browser]'\n"
                "    playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        # Fix-12: sandbox flag removed (was: "no-sandbox"). Chromium's sandbox works
        # correctly when the process runs as non-root inside a user namespace (userns)
        # or a properly configured container. That flag disables the seccomp/namespace-
        # based renderer sandbox entirely, which is unacceptable for a replay driver
        # that processes untrusted page content.
        #
        # If your environment lacks kernel user namespaces, enable them with:
        #     sysctl -w kernel.unprivileged_userns_clone=1
        # or: echo 'kernel.unprivileged_userns_clone=1' >> /etc/sysctl.d/99-hermes.conf
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            # On the baked OS the bundled Playwright Chromium is removed (dedup vs
            # the system RPM); use the system Chromium via env. Unset (dev/CI) →
            # None → Playwright uses its own managed browser unchanged.
            executable_path=os.environ.get("HERMES_CHROMIUM_EXECUTABLE") or None,
            # --password-store=basic + mock keychain: sin llavero GNOME (autologin
            # passwordless pediría su contraseña al abrir Chromium). Cero prompt.
            args=[
                "--disable-dev-shm-usage",
                "--password-store=basic",
                "--use-mock-keychain",
            ],
        )
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": self._viewport[0], "height": self._viewport[1]},
            "ignore_https_errors": self._ignore_https_errors,
        }
        if self._user_agent:
            context_kwargs["user_agent"] = self._user_agent
        if self._cert_pem_path:
            context_kwargs["client_certificates"] = [
                {
                    "origin": "*",  # se aplica a todos los origins; el caller filtra via URL
                    "certPath": self._cert_pem_path,
                    "passphrase": self._cert_pem_password or "",
                }
            ]
        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.playwright_close_failed",
                extra={"error": str(exc)},
            )

    async def execute(  # noqa: PLR0911  (dispatch por kind/payload)
        self,
        step: Step,
        *,
        hitl_approval_token: str | None = None,  # noqa: ARG002
    ) -> StepOutcome:
        if self._page is None:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="playwright_not_started_call_start_first",
            )

        try:
            if step.kind == StepKind.NAVIGATE:
                url = str(step.payload.get("url", ""))
                await self._page.goto(url, timeout=self._timeout_ms)
                return StepOutcome.ok(
                    step_id=step.step_id,
                    duration_ms=0,
                    result={"url": self._page.url},
                )

            if step.kind == StepKind.ACT:
                return await self._dispatch_act(step)

            if step.kind == StepKind.EXTRACT:
                selector = str(step.payload.get("selector", ""))
                attr = str(step.payload.get("attr", "text"))
                if not selector:
                    return StepOutcome.failed(
                        step_id=step.step_id, error="extract_requires_selector"
                    )
                locator = self._page.locator(selector)
                if attr == "text":
                    value = await locator.inner_text(timeout=self._timeout_ms)
                elif attr == "value":
                    value = await locator.input_value(timeout=self._timeout_ms)
                else:
                    value = await locator.get_attribute(attr, timeout=self._timeout_ms)
                return StepOutcome.ok(
                    step_id=step.step_id,
                    duration_ms=0,
                    result={"selector": selector, "attr": attr, "value": value},
                )

            if step.kind == StepKind.SCREENSHOT:
                full_page = bool(step.payload.get("full_page", False))
                _ = await self._page.screenshot(full_page=full_page)
                return StepOutcome.ok(step_id=step.step_id, duration_ms=0)

            if step.kind == StepKind.WAIT:
                state = str(step.payload.get("state", "networkidle"))
                await self._page.wait_for_load_state(state, timeout=self._timeout_ms)
                return StepOutcome.ok(step_id=step.step_id, duration_ms=0)

            return StepOutcome.failed(
                step_id=step.step_id,
                error=f"step kind {step.kind} no implementado en PlaywrightDriver",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.playwright_step_failed",
                extra={
                    "step_id": str(step.step_id),
                    "kind": step.kind,
                    "error": str(exc),
                },
            )
            return StepOutcome.failed(step_id=step.step_id, error=str(exc))

    async def _dispatch_act(self, step: Step) -> StepOutcome:
        payload = step.payload
        if "click_selector" in payload:
            selector = str(payload["click_selector"])
            await self._page.locator(selector).click(timeout=self._timeout_ms)
            return StepOutcome.ok(
                step_id=step.step_id, duration_ms=0, result={"clicked": selector}
            )
        if "fill_selector" in payload:
            selector = str(payload["fill_selector"])
            value = str(payload.get("fill_value", ""))
            await self._page.locator(selector).fill(value, timeout=self._timeout_ms)
            return StepOutcome.ok(
                step_id=step.step_id,
                duration_ms=0,
                result={"filled": selector, "len": len(value)},
            )
        if "select_selector" in payload:
            selector = str(payload["select_selector"])
            value = str(payload.get("select_value", ""))
            await self._page.locator(selector).select_option(value, timeout=self._timeout_ms)
            return StepOutcome.ok(
                step_id=step.step_id,
                duration_ms=0,
                result={"selected": selector, "value": value},
            )
        if "press_key" in payload:
            key = str(payload["press_key"])
            await self._page.keyboard.press(key)
            return StepOutcome.ok(step_id=step.step_id, duration_ms=0, result={"key": key})
        return StepOutcome.failed(
            step_id=step.step_id,
            error=(
                "ACT requiere uno de: click_selector / fill_selector / "
                "select_selector / press_key"
            ),
        )

    async def take_screenshot(self) -> bytes:
        if self._page is None:
            return b""
        return await self._page.screenshot()

    async def take_dom_snapshot(self) -> str:
        if self._page is None:
            return ""
        return await self._page.content()

    async def current_url(self) -> str:
        if self._page is None:
            return ""
        return str(self._page.url)
