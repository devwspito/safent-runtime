"""BrowserTab: value object que representa una pestaña del navegador.

T809 — US6/Phase 8 (domain layer).

Constitución I: BrowserTab es aditivo (no rompe contratos existentes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class BrowserTab:
    """Pestaña concreta del navegador dentro de una sesión.

    Atributos:
        tab_id:    identificador único de la pestaña en esta sesión.
        url:       URL actual (puede estar vacía si la tab aún no navegó).
        is_focused: True si esta es la pestaña activa para el siguiente step.
        opened_at: timestamp de apertura.
        closed_at: timestamp de cierre; None si aún está abierta.
    """

    tab_id: UUID
    url: str
    is_focused: bool = False
    opened_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    closed_at: datetime | None = None

    @classmethod
    def new(cls, *, url: str = "", is_focused: bool = False) -> BrowserTab:
        return cls(
            tab_id=uuid4(),
            url=url,
            is_focused=is_focused,
        )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def with_focused(self, focused: bool) -> BrowserTab:
        """Devuelve copia con is_focused modificado (inmutable)."""
        return BrowserTab(
            tab_id=self.tab_id,
            url=self.url,
            is_focused=focused,
            opened_at=self.opened_at,
            closed_at=self.closed_at,
        )

    def with_closed(self) -> BrowserTab:
        """Devuelve copia marcada como cerrada."""
        return BrowserTab(
            tab_id=self.tab_id,
            url=self.url,
            is_focused=self.is_focused,
            opened_at=self.opened_at,
            closed_at=datetime.now(tz=UTC),
        )
