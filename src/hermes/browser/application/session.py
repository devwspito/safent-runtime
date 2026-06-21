"""BrowserSession: orquesta driver + recorder + HITL gate + anti-bot.

Una sesion = una operacion de un tenant (e.g. "presentar 303 del 2T del cliente
12345678Z"). Vive lo que dura la operacion (typically 30s-15min).

Contrato de uso desde una vertical:

    config = BrowserSessionConfig(
        tenant_id=...,
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        anti_bot_min_delay_ms=200,
        anti_bot_max_delay_ms=800,
        require_hitl_for_medium=False,
    )
    async with BrowserSession.open(
        config=config,
        driver=stagehand_driver,
        recorder=step_recorder,
        storage_state_port=postgres_port,
        storage_state_key=derived_32_bytes,
        site_id="aeat_sede",       # must match config.site_id
    ) as session:
        await session.navigate("https://prewww10.aeat.es/...")
        # ...
        outcome = await session.act("click submit", risk=StepRisk.HIGH,
                                    hitl_approval_token=token)

`BrowserSession` NUNCA ejecuta steps HIGH sin token HITL valido. Si el
consumer no provee token cuando es necesario, levanta `HitlApprovalRequired`.

StorageState: si se inyecta `storage_state_port` + `storage_state_key`, la
sesion intenta cargar el estado previo al abrir y lo persiste al cerrar
exitosamente.  Decrypt corruption → estado limpio (constitution IV / US2/AC4).

Anti-bot: delay lognormal entre steps. Configurable; default mean=400ms
(percentil 5 ~150ms, percentil 95 ~1100ms).
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from hermes.browser.application.step_recorder import StepRecorder
from hermes.browser.domain.port import BrowserPort
from hermes.browser.domain.step import (
    Step,
    StepKind,
    StepOutcome,
    StepRisk,
    StepStatus,
)

if TYPE_CHECKING:
    from hermes.browser.domain.ports.storage_state_port import StorageStatePort

logger = logging.getLogger(__name__)


class HitlApprovalRequired(RuntimeError):
    """Step requiere HITL pero no se entrego token."""


@dataclass(frozen=True, slots=True)
class BrowserSessionConfig:
    """Configuracion inmutable de una sesion.

    `tenant_id`        : tenant emisor; va a audit + selector scoping.
    `site_id`          : sede operada ("aeat_sede", "tgss_red", "dehu", ...).
    `flow_id`          : flujo concreto ("modelo_303_borrador", "alta_red", ...).
    `anti_bot_*`       : truncated lognormal entre steps (ms).
    `require_hitl_for_medium`: si True, MEDIUM tambien atraviesa HITL gate.
                              Por defecto solo HIGH.
    `session_timeout_s`: hard timeout. Si excede, kill driver y reportar.
    """

    tenant_id: UUID
    site_id: str
    flow_id: str
    session_id: UUID = field(default_factory=uuid4)
    anti_bot_min_delay_ms: int = 150
    anti_bot_max_delay_ms: int = 1100
    anti_bot_mean_delay_ms: int = 400
    require_hitl_for_medium: bool = False
    session_timeout_s: int = 900
    capture_screenshot_pre: bool = True
    capture_screenshot_post: bool = True
    capture_dom: bool = True


class BrowserSession:
    """Orquestador de browser. Un instance = una operacion."""

    def __init__(
        self,
        *,
        config: BrowserSessionConfig,
        driver: BrowserPort,
        recorder: StepRecorder | None = None,
        storage_state_port: StorageStatePort | None = None,
        storage_state_key: bytes | None = None,
    ) -> None:
        self._config = config
        self._driver = driver
        self._recorder = recorder
        self._closed = False
        # StorageState wiring (all three required together).
        self._storage_port: StorageStatePort | None = storage_state_port
        self._storage_key: bytes | None = storage_state_key
        # Flag: True when the flow had a session expiration or corrupted state.
        # In that case we do NOT persist storage state on close.
        self._storage_invalidated: bool = False

    @property
    def config(self) -> BrowserSessionConfig:
        return self._config

    @property
    def driver(self) -> BrowserPort:
        return self._driver

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        *,
        config: BrowserSessionConfig,
        driver: BrowserPort,
        recorder: StepRecorder | None = None,
        storage_state_port: StorageStatePort | None = None,
        storage_state_key: bytes | None = None,
    ):  # type: ignore[no-untyped-def]  (asynccontextmanager)
        """Abre la sesion. Garantiza cierre del driver en `__aexit__`.

        StorageState: if ``storage_state_port`` + ``storage_state_key`` are
        both provided, the session attempts to restore the saved state before
        yielding, and persists the updated state inside the lock on close.

        US2/AC4: decrypt corruption → clean state, log, continue (no abort).
        Constitution IV: InvalidTag → invalidate + clean state, not swallowed.
        Constitution I: only optional kwargs added; existing callers unaffected.
        """
        session = cls(
            config=config,
            driver=driver,
            recorder=recorder,
            storage_state_port=storage_state_port,
            storage_state_key=storage_state_key,
        )
        await session._restore_storage_state()
        try:
            yield session
        finally:
            await session._persist_storage_state()
            await session.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._driver.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.driver_close_failed",
                extra={
                    "error": str(exc),
                    "session_id": str(self._config.session_id),
                },
            )

    # ------------------------------------------------------------------
    # StorageState lifecycle helpers
    # ------------------------------------------------------------------

    async def _restore_storage_state(self) -> None:
        """Load + decrypt StorageState and attach to driver if available."""
        if not self._has_storage_wiring():
            return
        port = self._storage_port  # type: ignore[assignment]
        key = self._storage_key  # type: ignore[assignment]
        tenant_id = self._config.tenant_id
        site_id = self._config.site_id

        from hermes.browser.domain.ports.storage_state_port import (  # noqa: PLC0415
            StorageStateInvalidationReason,
        )
        from hermes.browser.infrastructure.storage_state_crypto import (  # noqa: PLC0415
            decrypt_state,
        )

        state = await port.load(tenant_id=tenant_id, site_id=site_id)
        if state is None:
            logger.debug(
                "hermes.browser.storage_state.no_saved_state",
                extra={"tenant_id": str(tenant_id), "site_id": site_id},
            )
            return

        try:
            plaintext = decrypt_state(state, key=key)
        except Exception:  # noqa: BLE001 — cryptography.InvalidTag or similar
            # Constitution IV: fail-closed. Invalidate + proceed with clean state.
            logger.warning(
                "hermes.browser.storage_state.decrypt_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "site_id": site_id,
                    # kid not logged in user-facing paths (threat-model I1).
                },
            )
            await port.invalidate(
                tenant_id=tenant_id,
                site_id=site_id,
                reason=StorageStateInvalidationReason.CORRUPT_DECRYPT,
            )
            self._storage_invalidated = True
            return

        # Attach to driver (US2/AC2).
        try:
            await self._driver.attach_storage_state(plaintext)
        except AttributeError:
            # Driver does not support attach_storage_state (e.g. FakeBrowserDriver).
            logger.debug(
                "hermes.browser.storage_state.attach_not_supported",
                extra={"driver": getattr(self._driver, "driver_name", "unknown")},
            )

    async def _persist_storage_state(self) -> None:
        """Extract state from driver and save encrypted after successful flow."""
        if not self._has_storage_wiring():
            return
        if self._storage_invalidated:
            # Expiration or corruption occurred; do not overwrite.
            logger.debug(
                "hermes.browser.storage_state.skip_persist_invalidated",
                extra={"site_id": self._config.site_id},
            )
            return

        port = self._storage_port  # type: ignore[assignment]
        key = self._storage_key  # type: ignore[assignment]
        tenant_id = self._config.tenant_id
        site_id = self._config.site_id

        from hermes.browser.infrastructure.storage_state_crypto import (  # noqa: PLC0415
            encrypt_state,
        )

        try:
            plaintext = await self._driver.extract_storage_state()
        except AttributeError:
            # FakeBrowserDriver or driver without extract_storage_state.
            logger.debug(
                "hermes.browser.storage_state.extract_not_supported",
                extra={"driver": getattr(self._driver, "driver_name", "unknown")},
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.storage_state.extract_failed",
                extra={"error": str(exc), "site_id": site_id},
            )
            return

        try:
            encrypted = encrypt_state(
                plaintext,
                tenant_id=tenant_id,
                site_id=site_id,
                kid="default",
                key=key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.browser.storage_state.encrypt_failed",
                extra={"error": str(exc), "site_id": site_id},
            )
            return

        try:
            async with port.lock(tenant_id=tenant_id, site_id=site_id):
                await port.save(encrypted)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.browser.storage_state.save_failed",
                extra={"error": str(exc), "site_id": site_id},
            )

    def _has_storage_wiring(self) -> bool:
        """True if StorageState port + key are both provided."""
        return self._storage_port is not None and self._storage_key is not None

    # ------------------------------------------------------------------
    # High-level verbs
    # ------------------------------------------------------------------

    async def navigate(self, url: str, *, intent_desc: str = "") -> StepOutcome:
        step = self._make_step(
            kind=StepKind.NAVIGATE,
            risk=StepRisk.LOW,
            intent_desc=intent_desc or f"navigate to {url}",
            payload={"url": url},
        )
        return await self._execute(step, hitl_approval_token=None)

    async def observe(self, instruction: str) -> StepOutcome:
        step = self._make_step(
            kind=StepKind.OBSERVE,
            risk=StepRisk.LOW,
            intent_desc=instruction,
            payload={"instruction": instruction},
        )
        return await self._execute(step, hitl_approval_token=None)

    async def extract(
        self, *, instruction: str, schema: dict[str, Any]
    ) -> StepOutcome:
        step = self._make_step(
            kind=StepKind.EXTRACT,
            risk=StepRisk.LOW,
            intent_desc=instruction,
            payload={"instruction": instruction, "schema": schema},
        )
        return await self._execute(step, hitl_approval_token=None)

    async def act(
        self,
        instruction: str,
        *,
        risk: StepRisk = StepRisk.MEDIUM,
        fill_value: str | None = None,
        hitl_approval_token: str | None = None,
    ) -> StepOutcome:
        """Ejecuta un act: click, fill, scroll, select.

        `risk`:
          - LOW    -> no HITL (navegacion casual).
          - MEDIUM -> HITL solo si `config.require_hitl_for_medium=True`.
          - HIGH   -> HITL siempre. `hitl_approval_token` obligatorio.
        """
        payload: dict[str, Any] = {"instruction": instruction}
        if fill_value is not None:
            payload["fill_value"] = fill_value
        step = self._make_step(
            kind=StepKind.ACT,
            risk=risk,
            intent_desc=instruction,
            payload=payload,
        )
        return await self._execute(step, hitl_approval_token=hitl_approval_token)

    # ------------------------------------------------------------------
    # Internal execution
    # ------------------------------------------------------------------

    async def _execute(
        self,
        step: Step,
        *,
        hitl_approval_token: str | None,
    ) -> StepOutcome:
        if self._closed:
            return StepOutcome.failed(step_id=step.step_id, error="session_closed")

        if self._needs_hitl(step) and not hitl_approval_token:
            raise HitlApprovalRequired(
                f"step {step.step_id} (kind={step.kind}, risk={step.risk}) "
                "requiere HITL approval token; el consumer debe haberlo "
                "obtenido antes via la cola HITL del kernel."
            )

        # Anti-bot delay ANTES de ejecutar.
        await self._anti_bot_sleep()

        # PRE-snapshot.
        if self._recorder is not None and (
            self._config.capture_screenshot_pre or self._config.capture_dom
        ):
            await self._capture_pre(step)

        # Ejecutar.
        try:
            outcome = await asyncio.wait_for(
                self._driver.execute(step, hitl_approval_token=hitl_approval_token),
                timeout=self._config.session_timeout_s,
            )
        except TimeoutError:
            outcome = StepOutcome.failed(step_id=step.step_id, error="step_timeout")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.browser.driver_exception",
                extra={
                    "step_id": str(step.step_id),
                    "driver": self._driver.driver_name,
                    "error": str(exc),
                },
            )
            outcome = StepOutcome.failed(step_id=step.step_id, error=str(exc))

        # POST-snapshot.
        if self._recorder is not None:
            await self._capture_post(step, outcome)

        # Expiration detection: runs after NAVIGATE so URL + DOM are available.
        # FR-005: if expired → invalidate storage + raise OperatorReauthRequired.
        # NO autologin path exists.
        if step.kind == StepKind.NAVIGATE and self._has_storage_wiring():
            await self._check_expiration_after_navigate()

        return outcome

    async def _check_expiration_after_navigate(self) -> None:
        """Check for remote session expiration after a navigate step.

        Queries driver for current DOM + URL; runs expiration heuristic.
        If expired: invalidate storage + raise OperatorReauthRequired.
        FR-005: never attempts autologin. Constitution IV: fail-closed.
        """
        from hermes.browser.application.expiration_detector import (  # noqa: PLC0415
            detect_expired,
        )
        from hermes.browser.domain.ports.storage_state_port import (  # noqa: PLC0415
            OperatorReauthRequired,
            StorageStateInvalidationReason,
        )

        try:
            dom_text = await self._driver.take_dom_snapshot()
            url = getattr(self._driver, "current_url", "") or ""
        except Exception:  # noqa: BLE001
            # If we can't get DOM/URL, skip detection (do not false-positive).
            return

        if not detect_expired(dom_text, url):
            return

        # Session has expired on the remote site.
        self._storage_invalidated = True

        port = self._storage_port  # type: ignore[assignment]
        await port.invalidate(
            tenant_id=self._config.tenant_id,
            site_id=self._config.site_id,
            reason=StorageStateInvalidationReason.EXPIRED_REMOTE,
        )

        logger.warning(
            "hermes.browser.session.expired_remote",
            extra={
                "session_id": str(self._config.session_id),
                "site_id": self._config.site_id,
                "url": url,
            },
        )

        raise OperatorReauthRequired("EXPIRED_REMOTE")

    def _needs_hitl(self, step: Step) -> bool:
        if step.risk == StepRisk.HIGH:
            return True
        return bool(
            step.risk == StepRisk.MEDIUM and self._config.require_hitl_for_medium
        )

    async def _capture_pre(self, step: Step) -> None:
        if self._recorder is None:
            return
        try:
            screenshot = (
                await self._driver.take_screenshot()
                if self._config.capture_screenshot_pre
                else b""
            )
            dom = (
                await self._driver.take_dom_snapshot()
                if self._config.capture_dom
                else ""
            )
            await self._recorder.record_pre(step, screenshot=screenshot, dom_text=dom)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.capture_pre_failed",
                extra={"step_id": str(step.step_id), "error": str(exc)},
            )

    async def _capture_post(self, step: Step, outcome: StepOutcome) -> None:
        if self._recorder is None:
            return
        try:
            screenshot = (
                await self._driver.take_screenshot()
                if outcome.status == StepStatus.EXECUTED_OK
                and self._config.capture_screenshot_post
                else None
            )
            dom = (
                await self._driver.take_dom_snapshot()
                if outcome.status == StepStatus.EXECUTED_OK
                and self._config.capture_dom
                else None
            )
            await self._recorder.record_post(
                step, outcome, screenshot=screenshot, dom_text=dom
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.capture_post_failed",
                extra={"step_id": str(step.step_id), "error": str(exc)},
            )

    async def _anti_bot_sleep(self) -> None:
        delay_ms = _lognormal_delay_ms(
            min_ms=self._config.anti_bot_min_delay_ms,
            max_ms=self._config.anti_bot_max_delay_ms,
            mean_ms=self._config.anti_bot_mean_delay_ms,
        )
        await asyncio.sleep(delay_ms / 1000)

    def _make_step(
        self,
        *,
        kind: StepKind,
        risk: StepRisk,
        intent_desc: str,
        payload: dict[str, Any],
    ) -> Step:
        return Step.new(
            tenant_id=self._config.tenant_id,
            session_id=self._config.session_id,
            kind=kind,
            risk=risk,
            intent_desc=intent_desc,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Anti-bot delay sampling: truncated lognormal
# ---------------------------------------------------------------------------


def _lognormal_delay_ms(*, min_ms: int, max_ms: int, mean_ms: int) -> int:
    """Sample lognormal centered on `mean_ms`, clipped to [min_ms, max_ms].

    Lognormal modela mejor pausas humanas que uniform o normal: tail derecha
    larga (ocasionalmente "pienso", "leo", "verifico") y minimo en ~150ms.
    `secrets.SystemRandom` para no dejar huella de seed predecible.
    """
    if min_ms < 0 or max_ms < min_ms or mean_ms < min_ms:
        # Fallback seguro: 200-800 default.
        min_ms, max_ms, mean_ms = 200, 800, 400
    rng = secrets.SystemRandom()
    # mu = ln(mean) - sigma^2/2; sigma fijado en 0.55 para tail razonable.
    sigma = 0.55
    mu = math.log(max(mean_ms, 1)) - (sigma**2) / 2
    raw = rng.lognormvariate(mu, sigma)
    return int(max(min_ms, min(max_ms, raw)))
