"""T602 — Downgrade protection en Selector registry.

Threat-model control P3 #10 / E1 superficie 4.

Happy path: active.version >= max(deprecated.version) -> fetch_latest devuelve
el selector activo normalmente.

Ataque insider: v1 activo + v2 deprecated (alguien con DB write marca el nuevo
como deprecated y deja el viejo activo) -> fetch_latest detecta la inversion de
versiones, emite selector_inversion event, y devuelve None (fail-closed).
Discovery se dispara como si no existiera selector.

Constitution IV: fail-closed ante cualquier inversion de versiones.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from hermes.browser.domain.selector import Selector, SelectorStrategy
from hermes.browser.infrastructure import (
    InMemorySelectorRegistry,
    SignedSelectorRegistry,
    StoredSelector,
    build_signed,
)

_KEY = b"\xab\xcd" * 16

_SITE = "aeat_sede"
_FLOW = "modelo_303"
_STEP = "btn_presentar"


def _make_stored(
    *,
    version: int,
    deprecated: bool = False,
    key: bytes = _KEY,
) -> StoredSelector:
    """Crea un StoredSelector con firma valida para la key dada."""
    stored = build_signed(
        signing_key=key,
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        strategy=SelectorStrategy.CSS,
        value=f"#btn-v{version}",
        intent_desc=f"boton version {version}",
        version=version,
    )
    if deprecated:
        # Reemplazar el selector con deprecated_at seteado.
        # La firma sigue valida: deprecated_at no esta en el payload HMAC.
        selector_dep = Selector(
            selector_id=stored.selector.selector_id,
            site_id=stored.selector.site_id,
            flow_id=stored.selector.flow_id,
            step_id=stored.selector.step_id,
            strategy=stored.selector.strategy,
            value=stored.selector.value,
            intent_desc=stored.selector.intent_desc,
            version=stored.selector.version,
            author=stored.selector.author,
            deprecated_at=datetime.now(tz=UTC),
        )
        return StoredSelector(selector=selector_dep, signature_hex=stored.signature_hex)
    return stored


# ---------------------------------------------------------------------------
# Happy path: version activa >= max(deprecated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_active_version_is_highest_returns_selector() -> None:
    """Happy path: v1 deprecated, v2 activo -> v2 devuelto sin problema.

    Invariante cumplido: active.version (2) >= max(deprecated.version) (1).
    """
    store = InMemorySelectorRegistry()

    v1_dep = _make_stored(version=1, deprecated=True)
    v2_active = _make_stored(version=2, deprecated=False)

    await store.persist(v1_dep)
    await store.persist(v2_active)

    # Construir el registry con clave valida
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)

    result = await registry.fetch_latest(
        site_id=_SITE, flow_id=_FLOW, step_id=_STEP
    )
    assert result is not None, "Happy path: debe devolver el selector v2 activo"
    assert result.version == 2


# ---------------------------------------------------------------------------
# Ataque insider: v1 activo + v2 deprecated -> inversion detectada, None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insider_attack_v1_active_v2_deprecated_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ataque insider: un actor con DB write marca v2 como deprecated y deja v1 activo.

    Ambas firmas son validas (son selectors historicos con firmas correctas).
    Sin embargo, active.version (1) < max(deprecated.version) (2) -> inversion.
    Resultado esperado:
      - selector_inversion event emitido (structlog WARNING).
      - fetch_latest devuelve None (fail-closed).
      - Discovery se dispara como si no existiera selector.

    Threat-model E1 superficie 4: downgrade attack documentado.
    Control P3 #10: invariante enforced aqui.
    """
    store = InMemorySelectorRegistry()

    # v1 ACTIVO (sin deprecated_at)
    v1_active = _make_stored(version=1, deprecated=False)
    # v2 DEPRECATED (el atacante lo marco asi)
    v2_deprecated = _make_stored(version=2, deprecated=True)

    # Insertar en orden natural: v1 primero, v2 segundo
    await store.persist(v1_active)
    await store.persist(v2_deprecated)

    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)

    with caplog.at_level(logging.WARNING, logger="hermes.browser"):
        result = await registry.fetch_latest(
            site_id=_SITE, flow_id=_FLOW, step_id=_STEP
        )

    assert result is None, (
        "Ataque de inversion: active.version (1) < max(deprecated.version) (2). "
        "fetch_latest debe retornar None (fail-closed)."
    )

    # Verificar que se emitio el evento de auditoria
    inversion_records = [
        r for r in caplog.records
        if "selector_inversion" in r.getMessage()
    ]
    assert inversion_records, (
        "Debe emitirse un WARNING con 'selector_inversion' para auditoria."
    )
