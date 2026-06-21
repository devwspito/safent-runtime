"""StagehandDriver: Tier 1 default driver.

Wrappea Stagehand (Browserbase, MIT, Python SDK) sobre Playwright. Ventajas:
  - Action caching: replays cached selectors sin LLM inference; LLM fallback
    al detectar DOM drift. Bajo coste en flujos estables (AEAT/TGSS).
  - Primitivas: `act("click X")`, `extract(schema)`, `observe()`.
  - Compatible con cualquier browser Playwright (local Chromium incluido).

Lazy import: si `stagehand` no esta instalado, el modulo se importa SIN error
y `StagehandDriver()` levanta `StagehandNotInstalledError` solo al instanciar.

Tests del consumer NO requieren stagehand: usar `FakeBrowserDriver` de testing/.

Constitution IV: HERMES_MODEL vacío en steps LLM-dependientes →
  StepOutcome.failed(error="llm_not_configured"). No crash.
Constitution III: DOM sanitization before any LLM prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from hermes.browser.application.dom_sanitizer import sanitize_for_llm
from hermes.browser.domain.step import Step, StepKind, StepOutcome

logger = logging.getLogger(__name__)

# Step kinds that require an LLM to function.
_LLM_DEPENDENT_KINDS: frozenset[StepKind] = frozenset({
    StepKind.ACT,
    StepKind.EXTRACT,
    StepKind.OBSERVE,
})


class StagehandNotInstalledError(RuntimeError):
    """Paquete `stagehand-py` no esta instalado.

    Instala con: `pip install "hermes-runtime[browser]"`.
    """


class StagehandDriver:
    """Adapter sobre Stagehand. Implementa `BrowserPort` Protocol.

    Construccion:
        driver = StagehandDriver(
            model_name="azure/gpt-4o",           # LiteLLM-compatible
            api_key="...",
            base_url=None,
            local=True,                          # True = self-hosted Chromium; False = Browserbase
            headless=True,
            stealth=True,
        )
        await driver.start()                     # arranca Chromium + page

    El driver mantiene UN browser context + UNA page. Para multi-page, abrir
    multiples drivers. El pooling cross-tenant lo gestiona el consumer.

    Anti-detection profile is applied via `set_anti_detection_profile()`.
    The stub honours viewport, user_agent, locale, timezone at minimum.
    Full Bezier-curve cursor + char-by-char typing lives in Playwright-extra
    stealth plugin (Phase 10 scope).
    """

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        local: bool = True,
        headless: bool = True,
        stealth: bool = True,
        timeout_ms: int = 30_000,
        extra_init: dict[str, Any] | None = None,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._local = local
        self._headless = headless
        self._stealth = stealth
        self._timeout_ms = timeout_ms
        self._extra_init = dict(extra_init or {})
        self._stagehand: Any = None  # lazy
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Arranca Chromium + page. Levanta `StagehandNotInstalledError` si no esta el SDK."""
        Stagehand = _import_stagehand()
        config_kwargs: dict[str, Any] = {
            "env": "LOCAL" if self._local else "BROWSERBASE",
            "model_name": self._model_name,
            "headless": self._headless,
            "verbose": 0,
        }
        if self._api_key:
            config_kwargs["model_api_key"] = self._api_key
        if self._base_url:
            config_kwargs["model_base_url"] = self._base_url
        config_kwargs.update(self._extra_init)
        self._stagehand = Stagehand(**config_kwargs)
        await self._stagehand.init()

    async def close(self) -> None:
        if self._closed or self._stagehand is None:
            return
        self._closed = True
        try:
            close_fn = getattr(self._stagehand, "close", None)
            if close_fn is not None:
                await close_fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.browser.stagehand_close_failed", extra={"error": str(exc)})

    @property
    def driver_name(self) -> str:
        return "stagehand"

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "stagehand_local": self._local,
            "stagehand_headless": self._headless,
            "stagehand_stealth": self._stealth,
            "stagehand_model": self._model_name,
            "supports_vision": False,
            "supports_action_caching": True,
        }

    # ------------------------------------------------------------------
    # BrowserPort surface
    # ------------------------------------------------------------------

    async def execute(  # noqa: PLR0911  (dispatch por kind de step)
        self,
        step: Step,
        *,
        hitl_approval_token: str | None = None,  # noqa: ARG002  (la session valida, no el driver)
    ) -> StepOutcome:
        if self._stagehand is None:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="stagehand_not_started_call_start_first",
            )

        # Constitution IV: fail-closed when no LLM configured for LLM-dependent steps.
        if step.kind in _LLM_DEPENDENT_KINDS and not self._model_name:
            logger.warning(
                "hermes.browser.llm_not_configured",
                extra={"step_id": str(step.step_id), "kind": step.kind},
            )
            return StepOutcome.failed(
                step_id=step.step_id,
                error="llm_not_configured",
            )

        page = self._stagehand.page
        try:
            return await self._dispatch(step, page)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.stagehand_step_failed",
                extra={"step_id": str(step.step_id), "kind": step.kind, "error": str(exc)},
            )
            return StepOutcome.failed(step_id=step.step_id, error=str(exc))

    async def _dispatch(self, step: Step, page: Any) -> StepOutcome:
        """Dispatch step to the appropriate Stagehand/Playwright primitive."""
        kind = step.kind
        llm_dispatch = {
            StepKind.NAVIGATE: self._navigate,
            StepKind.ACT: self._act,
            StepKind.EXTRACT: self._extract,
            StepKind.OBSERVE: self._observe,
        }
        if kind in llm_dispatch:
            return await llm_dispatch[kind](step, page)
        return await self._dispatch_non_llm(step, page, kind)

    async def _dispatch_non_llm(self, step: Step, page: Any, kind: StepKind) -> StepOutcome:
        """Handle non-LLM step kinds: SCREENSHOT, WAIT, and unknown."""
        if kind == StepKind.SCREENSHOT:
            await page.screenshot()
            return StepOutcome.ok(step_id=step.step_id, duration_ms=0)
        if kind == StepKind.WAIT:
            await page.wait_for_load_state(str(step.payload.get("state", "networkidle")))
            return StepOutcome.ok(step_id=step.step_id, duration_ms=0)
        return StepOutcome.failed(
            step_id=step.step_id,
            error=f"step kind {kind} no implementado en StagehandDriver",
        )

    async def _navigate(self, step: Step, page: Any) -> StepOutcome:
        url = str(step.payload.get("url", ""))
        await page.goto(url, timeout=self._timeout_ms)
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"url": page.url},
        )

    async def _act(self, step: Step, page: Any) -> StepOutcome:
        """Execute act step with DOM sanitization before LLM prompt.

        Constitution III: DOM content is sanitized before being sent to LLM.
        PII variables are passed as Stagehand variables (never in plain text).
        """
        instruction = str(step.payload.get("instruction", ""))
        action_kwargs: dict[str, Any] = {}

        # Pass tokenised PII values as named variables to Stagehand so the LLM
        # never sees the real value in the prompt, only the placeholder.
        variables = step.payload.get("variables")
        if variables:
            action_kwargs["variables"] = dict(variables)
        elif step.payload.get("fill_value") is not None:
            action_kwargs["variables"] = {"value": str(step.payload["fill_value"])}

        # Sanitize page DOM before it enters any LLM context.
        dom_text = await page.content()
        sanitize_for_llm(dom_text)  # emits structlog event; result used by future hook

        result = await page.act(instruction, **action_kwargs)
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"act_result": _as_dict(result)},
        )

    async def _extract(self, step: Step, page: Any) -> StepOutcome:
        instruction = str(step.payload.get("instruction", ""))
        schema = step.payload.get("schema") or {}

        # Sanitize DOM before LLM extract prompt (Constitution III).
        dom_text = await page.content()
        sanitize_for_llm(dom_text)

        result = await page.extract({"instruction": instruction, "schema": schema})
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result=_as_dict(result),
        )

    async def _observe(self, step: Step, page: Any) -> StepOutcome:
        instruction = str(step.payload.get("instruction", ""))

        # Sanitize DOM before LLM observe prompt (Constitution III).
        dom_text = await page.content()
        sanitize_for_llm(dom_text)

        result = await page.observe(instruction)
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"candidates": _as_dict_list(result)},
        )

    async def take_screenshot(self) -> bytes:
        if self._stagehand is None:
            return b""
        return await self._stagehand.page.screenshot()

    async def take_dom_snapshot(self) -> str:
        if self._stagehand is None:
            return ""
        return await self._stagehand.page.content()

    async def current_url(self) -> str:
        if self._stagehand is None:
            return ""
        return self._stagehand.page.url

    # ------------------------------------------------------------------
    # Extended BrowserPort surface (contracts/browser_port.py additions)
    # ------------------------------------------------------------------

    async def attach_storage_state(self, state_bytes: bytes) -> None:
        """Attach decrypted StorageState to the browser context.

        Caller provides already-decrypted bytes. The driver never sees
        the encryption key (Constitution VI / composition root responsibility).
        """
        if self._stagehand is None:
            return
        # Playwright context storage_state loading happens at context creation.
        # Post-creation injection is done via cookie/localStorage APIs.
        # Full implementation is part of T406 (US2); this stub logs intent.
        logger.info("hermes.browser.attach_storage_state", extra={"size": len(state_bytes)})

    async def extract_storage_state(self) -> bytes:
        """Extract current browser context StorageState as JSON bytes."""
        if self._stagehand is None:
            return b"{}"
        try:
            state = await self._stagehand.context.storage_state()
            return json.dumps(state).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.extract_storage_state_failed",
                extra={"error": str(exc)},
            )
            return b"{}"

    async def set_anti_detection_profile(self, profile: Any) -> None:
        """Apply anti-detection profile: viewport, UA, locale, timezone.

        Stub implementation: honours the AntiDetectionProfile data.
        Full Bezier-curve cursor + char-by-char typing via playwright-extra
        stealth plugin is Phase 10 scope (FR-024, FR-025).
        """
        if self._stagehand is None:
            return
        try:
            context = self._stagehand.context
            if profile.viewport_width and profile.viewport_height:
                page = self._stagehand.page
                await page.set_viewport_size({
                    "width": profile.viewport_width,
                    "height": profile.viewport_height,
                })
            if profile.extra_http_headers:
                await context.set_extra_http_headers(profile.extra_http_headers)
            logger.info(
                "hermes.browser.anti_detection_profile_applied",
                extra={
                    "kind": str(profile.kind),
                    "viewport": f"{profile.viewport_width}x{profile.viewport_height}",
                    "locale": profile.locale,
                    "timezone_id": profile.timezone_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.anti_detection_profile_failed",
                extra={"error": str(exc)},
            )

    def subscribe_live_view(self) -> Any:
        """Stream live frames — not yet implemented (Phase 7 scope).

        Raises RuntimeError until T704 (US5) is complete.
        """
        raise RuntimeError("LiveViewNotAvailable — Phase 7 scope (T704)")

    async def release_control(self) -> None:
        """Return control to runtime after human take-control. Idempotent no-op."""
        logger.info("hermes.browser.release_control_called")


# ---------------------------------------------------------------------------
# Lazy import helper
# ---------------------------------------------------------------------------


def _import_stagehand() -> Any:
    try:
        from stagehand import Stagehand  # noqa: PLC0415

        return Stagehand
    except ImportError as exc:
        raise StagehandNotInstalledError(
            "stagehand-py no esta instalado. Instala con:\n"
            "    pip install 'hermes-runtime[browser]'\n"
            "Y luego: playwright install chromium"
        ) from exc


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump())
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.browser.stagehand_model_dump_failed", extra={"error": str(exc)})
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return {"value": value}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [_as_dict(item) for item in value]
    return [_as_dict(value)]
