"""Selector + SelectorRegistry: localizadores estables de elementos DOM.

Diseño:
  - Cada Selector identifica un elemento en una pagina concreta de una sede.
  - Versionado: cuando el LLM "descubre" un selector nuevo o uno antiguo
    falla, se persiste una nueva version con `created_at`. El previo se
    marca `deprecated_at`.
  - HMAC: el selector se firma con la master key del tenant (anti-tampering
    a nivel DB). Si alguien edita la columna `selector_json` saltandose el
    repository, la firma deja de validar -> runtime lo descarta.
  - author: quien produjo el selector (seed manual, LLM discovery, o
    intervencion del operador humano). El author se incluye en el payload
    HMAC para que cualquier modificacion externa del campo sea detectable.

El Protocol `SelectorRegistry` lo implementan dos adapters:
  - `SignedSelectorRegistry` (Postgres-backed con HMAC, infrastructure/).
  - `InMemorySelectorRegistry` (testing, sin Postgres).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4


class SelectorAuthor(StrEnum):
    """Quien produjo este Selector.

    Importante para auditoria y para ponderar confianza:
      - SEED                  : sembrado manualmente (admin / fixture).
      - LLM_DISCOVERY         : descubierto por discovery o self-healing.
      - OPERATOR_INTERVENTION : extraido de una OperatorIntervention.

    El author se incluye en el payload HMAC (firma v2). Cambiar el author
    en la DB rompe la firma -> deteccion de tampering (T604).
    """

    SEED = "seed"
    LLM_DISCOVERY = "llm_discovery"
    OPERATOR_INTERVENTION = "operator_intervention"


class SelectorStrategy(StrEnum):
    """Como localizar el elemento.

    PLAYWRIGHT_GETBY: `page.get_by_role("button", name=...)` / Stagehand act.
    CSS:              `page.locator("css=#btn-303")`.
    XPATH:            `page.locator("xpath=//button[@id='btn-303']")`.
    ACCESSIBILITY_REF: ref enumerado por DOM distilled (Stagehand / browser-use).
    TEXT:             `page.get_by_text("Presentar definitivo")`.
    """

    PLAYWRIGHT_GETBY = "playwright_getby"
    CSS = "css"
    XPATH = "xpath"
    ACCESSIBILITY_REF = "a11y_ref"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Selector:
    """Localizador inmutable de un elemento.

    Atributos:
        selector_id:   identifier de esta version.
        site_id:       sede (ej. "aeat_sede", "tgss_red", "dehu").
        flow_id:       flujo dentro de la sede (ej. "modelo_303_borrador").
        step_id:       step dentro del flujo (ej. "btn_presentar_definitivo").
        strategy:      como ubicar.
        value:         expresion concreta (selector CSS / texto / ref).
        intent_desc:   NL — "boton de presentar definitivo del 303".
        tenant_scope:  si esta atado a un tenant concreto o `None` (global).
        version:       monoton creciente por (site, flow, step).
        created_at:    cuando se materializo esta version.
        deprecated_at: timestamp cuando se descubrio que ya no funciona.
        last_seen_ok:  ultima vez que el step lo uso con exito.
    """

    selector_id: UUID
    site_id: str
    flow_id: str
    step_id: str
    strategy: SelectorStrategy
    value: str
    intent_desc: str
    tenant_scope: UUID | None = None
    version: int = 1
    author: SelectorAuthor = SelectorAuthor.LLM_DISCOVERY
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    deprecated_at: datetime | None = None
    last_seen_ok: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        strategy: SelectorStrategy,
        value: str,
        intent_desc: str,
        tenant_scope: UUID | None = None,
        version: int = 1,
        author: SelectorAuthor = SelectorAuthor.LLM_DISCOVERY,
    ) -> Selector:
        return cls(
            selector_id=uuid4(),
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            strategy=strategy,
            value=value,
            intent_desc=intent_desc,
            tenant_scope=tenant_scope,
            version=version,
            author=author,
        )

    @property
    def is_active(self) -> bool:
        return self.deprecated_at is None


class SelectorRegistry(Protocol):
    """Contrato del repositorio de selectores."""

    async def fetch_latest(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None = None,
    ) -> Selector | None:
        """Devuelve el selector vigente para (site, flow, step) o None."""
        ...

    async def history(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None = None,
    ) -> Sequence[Selector]:
        """Devuelve todas las versiones (incluidas deprecated) ordenadas asc."""
        ...

    async def persist(self, selector: Selector) -> None:
        """Guarda una nueva version. Marca la previa como deprecated."""
        ...

    async def mark_deprecated(
        self, selector_id: UUID, *, reason: str = ""
    ) -> None:
        """El selector dejo de funcionar. Subsequent reads no lo devuelven."""
        ...

    async def touch_ok(self, selector_id: UUID) -> None:
        """Marca que el selector funciono OK (actualiza last_seen_ok)."""
        ...
