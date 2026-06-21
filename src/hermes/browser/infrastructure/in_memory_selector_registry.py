"""InMemorySelectorRegistry: implementacion in-memory de `SelectorStore`.

Util para tests y para sandbox local sin Postgres. Mantiene la firma HMAC
del `SignedSelectorRegistry` (puedes envolver una con la otra).

NO usar en produccion multi-tenant: no persiste y no es thread-safe.

T602 downgrade protection: fetch_latest verifica que el selector activo tiene
version >= max(deprecated.version). Si se viola (ataque insider que marca el
nuevo como deprecated y el viejo como activo), emite selector_inversion y
devuelve None (fail-closed, dispara discovery).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from hermes.browser.domain.selector import Selector
from hermes.browser.infrastructure.signed_selector_registry import (
    StoredSelector,
)

logger = logging.getLogger(__name__)


class InMemorySelectorRegistry:
    """`SelectorStore` in-memory (dict). La firma vive en SignedSelectorRegistry."""

    def __init__(self) -> None:
        # key = (site_id, flow_id, step_id, tenant_scope) -> list[StoredSelector]
        self._rows: dict[
            tuple[str, str, str, UUID | None], list[StoredSelector]
        ] = {}

    async def fetch_latest(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None,
    ) -> StoredSelector | None:
        key = (site_id, flow_id, step_id, tenant_scope)
        history = self._rows.get(key, [])
        active = [s for s in history if s.selector.deprecated_at is None]
        deprecated = [s for s in history if s.selector.deprecated_at is not None]

        if not active:
            return history[-1] if history else None

        best = max(active, key=lambda s: s.selector.version)

        if deprecated:
            max_deprecated_version = max(s.selector.version for s in deprecated)
            if best.selector.version < max_deprecated_version:
                logger.warning(
                    "hermes.browser.selector.selector_inversion",
                    extra={
                        "site_id": site_id,
                        "flow_id": flow_id,
                        "step_id": step_id,
                        "active_version": best.selector.version,
                        "max_deprecated_version": max_deprecated_version,
                    },
                )
                return None

        return best

    async def history(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None,
    ) -> Sequence[StoredSelector]:
        key = (site_id, flow_id, step_id, tenant_scope)
        return tuple(sorted(self._rows.get(key, []), key=lambda s: s.selector.version))

    async def persist(self, stored: StoredSelector) -> None:
        selector = stored.selector
        key = (selector.site_id, selector.flow_id, selector.step_id, selector.tenant_scope)
        self._rows.setdefault(key, []).append(stored)

    async def mark_deprecated_by_id(
        self, selector_id: UUID, *, reason: str
    ) -> None:
        _ = reason  # no se persiste motivo en in-memory; OK para tests
        for history in self._rows.values():
            for i, stored in enumerate(history):
                if stored.selector.selector_id == selector_id:
                    if stored.selector.deprecated_at is not None:
                        return
                    updated_selector = _with_deprecated(stored.selector)
                    # La firma sigue valida: solo cambian campos no-firmados.
                    history[i] = StoredSelector(
                        selector=updated_selector,
                        signature_hex=stored.signature_hex,
                    )
                    return

    async def touch_ok_by_id(self, selector_id: UUID) -> None:
        now = datetime.now(tz=UTC)
        for history in self._rows.values():
            for i, stored in enumerate(history):
                if stored.selector.selector_id == selector_id:
                    history[i] = StoredSelector(
                        selector=_with_last_seen_ok(stored.selector, now),
                        signature_hex=stored.signature_hex,
                    )
                    return


def _with_deprecated(s: Selector) -> Selector:
    return Selector(
        selector_id=s.selector_id,
        site_id=s.site_id,
        flow_id=s.flow_id,
        step_id=s.step_id,
        strategy=s.strategy,
        value=s.value,
        intent_desc=s.intent_desc,
        tenant_scope=s.tenant_scope,
        version=s.version,
        author=s.author,
        created_at=s.created_at,
        deprecated_at=datetime.now(tz=UTC),
        last_seen_ok=s.last_seen_ok,
        metadata=dict(s.metadata),
    )


def _with_last_seen_ok(s: Selector, ts: datetime) -> Selector:
    return Selector(
        selector_id=s.selector_id,
        site_id=s.site_id,
        flow_id=s.flow_id,
        step_id=s.step_id,
        strategy=s.strategy,
        value=s.value,
        intent_desc=s.intent_desc,
        tenant_scope=s.tenant_scope,
        version=s.version,
        author=s.author,
        created_at=s.created_at,
        deprecated_at=s.deprecated_at,
        last_seen_ok=ts,
        metadata=dict(s.metadata),
    )
