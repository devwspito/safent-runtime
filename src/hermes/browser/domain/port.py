"""BrowserPort: contrato que cualquier driver de browser cumple.

Drivers concretos (infrastructure/):
  - PlaywrightDriver: Playwright bare. Util como base y para Tier 4 replay.
  - StagehandDriver:  Stagehand (Tier 1 default). Action caching + AI fallback.
  - BrowserUseDriver: browser-use (Tier 2 discovery). Opcional.
  - ComputerUseDriver: Anthropic computer-use (Tier 3 visual escape). Opcional.

El `BrowserSession` orquesta uno o varios drivers para una operacion concreta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from hermes.browser.domain.step import Step, StepOutcome


@dataclass(frozen=True, slots=True)
class LiveViewFrame:
    """Frame transitorio del LiveViewChannel.

    Contiene screenshot + DOM del estado actual del browser para el operador.
    NO se persiste por defecto. El campo screenshot_bytes contiene PII; el
    canal debe operar sobre transport autorizado (constitución III).

    timestamp: incluido como control anti-stale (T712). El adapter vertical
    debe rechazar frames con timestamp muy antiguo (>30s).
    """

    session_id: UUID
    tab_id: UUID
    screenshot_bytes: bytes
    dom_text: str
    url: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class BrowserPort(Protocol):
    """Contrato uniforme para drivers de browser.

    Invariantes:
      - `execute` NUNCA ejecuta steps con `requires_hitl=True` si no recibe
        un `hitl_approval_token` valido. El `BrowserSession` lo verifica.
      - El driver es stateless por sesion; el estado vive en el browser
        context (cookies, sessionStorage). Pool LRU lo gestiona el consumer.
      - `extract` retorna un dict JSON-serializable; el LLM ya tiene schema
        en `step.payload['schema']` y el driver fuerza conformance.
      - Captacion de errores: nunca propaga excepcion al consumidor; siempre
        devuelve `StepOutcome.failed(...)` con `error` explicativo.
    """

    async def execute(
        self,
        step: Step,
        *,
        hitl_approval_token: str | None = None,
    ) -> StepOutcome:
        """Ejecuta un step. Para steps HIGH requiere token HITL valido."""
        ...

    async def take_screenshot(self) -> bytes:
        """Captura screenshot PNG del viewport actual."""
        ...

    async def take_dom_snapshot(self) -> str:
        """Captura DOM accessibility tree distilled (text)."""
        ...

    async def current_url(self) -> str:
        """URL actual del navegador."""
        ...

    async def close(self) -> None:
        """Libera recursos del driver (browser context, page, etc.)."""
        ...

    @property
    def driver_name(self) -> str:
        """Identifier del driver, para audit y observability."""
        ...

    @property
    def capabilities(self) -> dict[str, Any]:
        """Metadata del driver: stagehand_version, supports_vision, etc."""
        ...
