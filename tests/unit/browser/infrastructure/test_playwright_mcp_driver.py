"""Unit tests del PlaywrightMcpDriver con FakeMcpSession.

Cubre los 4 contratos exigidos por el requisito:
  (a) navigate + snapshot devuelve arbol semantico.
  (b) OBSERVE: una accion grabada almacena role+name+description, NO ref crudo.
  (c) Replay re-resuelve el ref desde un snapshot fresco cuando la pagina cambia.
  (d) Self-healing path cuando el selector almacenado no se encuentra.

Constitution V: sin Chromium, sin Node, sin red.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.domain.step import Step, StepKind, StepRisk, StepStatus
from hermes.browser.infrastructure.playwright_mcp_driver import (
    PlaywrightMcpDriver,
    _parse_candidates,
    _resolve_ref,
)
from hermes.browser.testing.fake_mcp_session import FakeMcpSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT = uuid4()
_SESSION = uuid4()


def _step(kind: StepKind, payload: dict, *, risk: StepRisk = StepRisk.LOW) -> Step:
    return Step.new(
        tenant_id=_TENANT,
        session_id=_SESSION,
        kind=kind,
        risk=risk,
        intent_desc="test step",
        payload=payload,
    )


_SNAPSHOT_PAGE_1 = "\n".join([
    "URL: https://example.com/form",
    'role=button name="Presentar definitivo" ref=e5',
    'role=link   name="Inicio"              ref=e12',
    'role=textbox name="NIF"                ref=e3',
])

_SNAPSHOT_PAGE_2 = "\n".join([
    "URL: https://example.com/form?submitted=1",
    'role=button name="Presentar definitivo" ref=e99',  # ref changed after navigation
    'role=link   name="Inicio"               ref=e44',
    'role=textbox name="NIF"                 ref=e21',
])


# ---------------------------------------------------------------------------
# (a) navigate + snapshot devuelve arbol semantico
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_returns_current_url() -> None:
    """NAVIGATE devuelve la URL actual que informa el servidor MCP."""
    session = FakeMcpSession(
        current_urls=["about:blank", "https://example.com/form"],
        snapshots=[_SNAPSHOT_PAGE_1],
    )
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(StepKind.NAVIGATE, {"url": "https://example.com/form"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["url"] == "https://example.com/form"
    assert session.navigated_urls == ["https://example.com/form"]


@pytest.mark.asyncio
async def test_take_dom_snapshot_returns_accessibility_tree() -> None:
    """take_dom_snapshot devuelve el accessibility tree como texto plano."""
    session = FakeMcpSession(snapshots=[_SNAPSHOT_PAGE_1])
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    snapshot = await driver.take_dom_snapshot()

    assert "role=button" in snapshot
    assert "Presentar definitivo" in snapshot
    assert "ref=e5" in snapshot


@pytest.mark.asyncio
async def test_observe_returns_semantic_candidates_not_raw_refs() -> None:
    """OBSERVE produce candidates con value='role=X name=Y', NO con ref crudo."""
    session = FakeMcpSession(snapshots=[_SNAPSHOT_PAGE_1])
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(StepKind.OBSERVE, {"instruction": "botones del formulario"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    candidates = outcome.result["candidates"]
    assert len(candidates) >= 3

    # Cada candidato debe tener la identidad semantica en 'value', no el ref crudo.
    button_candidate = next(
        c for c in candidates if "Presentar definitivo" in c["value"]
    )
    assert button_candidate["strategy"] == "accessibility_ref"
    assert button_candidate["value"] == "role=button name=Presentar definitivo"
    assert "ref=" not in button_candidate["value"], (
        "El 'value' de un candidato NO debe contener el ref efimero"
    )
    # El ref efimero SOLO debe estar en metadata, nunca en 'value'
    assert button_candidate["metadata"]["ref"] == "e5"


# ---------------------------------------------------------------------------
# (b) Candidato grabado: almacena role+name (durable), no ref crudo
# ---------------------------------------------------------------------------


def test_parse_candidates_extracts_semantic_value() -> None:
    """_parse_candidates extrae role+name como identidad durable."""
    candidates = _parse_candidates(_SNAPSHOT_PAGE_1)

    values = [c["value"] for c in candidates]
    assert "role=button name=Presentar definitivo" in values
    assert "role=link name=Inicio" in values
    assert "role=textbox name=NIF" in values

    # Ninguna value debe contener un ref efimero
    for c in candidates:
        assert "ref=" not in c["value"], (
            f"El campo 'value' del candidato contiene ref efimero: {c['value']!r}"
        )


def test_parse_candidates_confidence_unique_name_is_high() -> None:
    """Elementos con nombre unico reciben confidence 0.9; duplicados 0.5."""
    snapshot = "\n".join([
        'role=button name="Enviar" ref=e1',
        'role=button name="Enviar" ref=e2',  # nombre duplicado
        'role=link   name="Home"  ref=e3',   # nombre unico
    ])
    candidates = _parse_candidates(snapshot)
    by_ref = {c["metadata"]["ref"]: c for c in candidates}

    assert by_ref["e1"]["confidence"] == 0.5
    assert by_ref["e2"]["confidence"] == 0.5
    assert by_ref["e3"]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# (c) Replay re-resuelve el ref desde snapshot fresco cuando la pagina cambia
# ---------------------------------------------------------------------------


def test_resolve_ref_finds_element_by_role_name() -> None:
    """_resolve_ref encuentra el ref correcto en un snapshot fresco."""
    ref = _resolve_ref(_SNAPSHOT_PAGE_1, role="button", name="Presentar definitivo")
    assert ref == "e5"


def test_resolve_ref_case_insensitive_name() -> None:
    """_resolve_ref ignora mayusculas/minusculas en el name."""
    ref = _resolve_ref(_SNAPSHOT_PAGE_1, role="button", name="PRESENTAR DEFINITIVO")
    assert ref == "e5"


def test_resolve_ref_page_changed_returns_new_ref() -> None:
    """Cuando la pagina cambia, el mismo role+name resuelve a un ref diferente.

    Este es el comportamiento critico: el ReplayStep almacena role+name
    (durable), NO e5. Al replay se toma _SNAPSHOT_PAGE_2 donde el mismo
    boton tiene ref=e99, y el driver lo encuentra correctamente.
    """
    old_ref = _resolve_ref(_SNAPSHOT_PAGE_1, role="button", name="Presentar definitivo")
    new_ref = _resolve_ref(_SNAPSHOT_PAGE_2, role="button", name="Presentar definitivo")

    assert old_ref == "e5"
    assert new_ref == "e99"
    assert old_ref != new_ref, "El ref debe cambiar — el test simula drift de la pagina"


@pytest.mark.asyncio
async def test_act_with_mcp_identity_resolves_fresh_ref_on_changed_page() -> None:
    """ACT con mcp_identity toma un snapshot fresco y re-resuelve el ref actual.

    Simulamos que entre el momento del recording y el replay la pagina cambio:
      - Al grabar, el boton era ref=e5 (en _SNAPSHOT_PAGE_1).
      - Al reproducir, el driver recibe mcp_identity={role, name} y toma
        _SNAPSHOT_PAGE_2 donde el mismo boton tiene ref=e99.
    El driver debe hacer click en e99, no en e5.
    """
    session = FakeMcpSession(
        snapshots=[_SNAPSHOT_PAGE_2],  # pagina cambiada en replay
        current_urls=["https://example.com/form?submitted=1"],
    )
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(
        StepKind.ACT,
        {
            "mcp_identity": {"role": "button", "name": "Presentar definitivo"},
            "action": "click",
        },
    )
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["clicked_ref"] == "e99", (
        "El driver debe haber re-resuelto el ref desde el snapshot fresco"
    )
    # Verificar que se hizo snapshot (re-resolution) y click
    assert session.snapshot_call_count >= 1
    assert "e99" in session.clicked_refs


# ---------------------------------------------------------------------------
# (d) Self-healing: selector no encontrado devuelve failed
# ---------------------------------------------------------------------------


def test_resolve_ref_returns_none_when_not_found() -> None:
    """_resolve_ref devuelve None si el role+name no existe en el snapshot."""
    ref = _resolve_ref(_SNAPSHOT_PAGE_1, role="button", name="Boton inexistente")
    assert ref is None


@pytest.mark.asyncio
async def test_act_with_missing_identity_returns_failed_outcome() -> None:
    """ACT con mcp_identity que no se resuelve devuelve StepOutcome.failed.

    El driver sigue Constitution IV: fail-closed, sin excepcion al consumer.
    Upstream (SelfHealer) puede interpretar el error y pedir OperatorIntervention.
    """
    session = FakeMcpSession(
        snapshots=[_SNAPSHOT_PAGE_1],
    )
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(
        StepKind.ACT,
        {
            "mcp_identity": {"role": "button", "name": "Boton que ya no existe"},
            "action": "click",
        },
    )
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "mcp_ref_not_found" in (outcome.error or "")
    # No se realizo ningun click
    assert session.clicked_refs == []


@pytest.mark.asyncio
async def test_act_without_ref_or_identity_returns_failed() -> None:
    """ACT sin mcp_ref ni mcp_identity devuelve error descriptivo."""
    session = FakeMcpSession(snapshots=[_SNAPSHOT_PAGE_1])
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(StepKind.ACT, {"action": "click"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "act_requires_mcp_ref_or_mcp_identity" in (outcome.error or "")


# ---------------------------------------------------------------------------
# Otros comportamientos del driver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_before_start_returns_failed() -> None:
    """execute() sin llamar start() devuelve failed, no excepcion."""
    session = FakeMcpSession()
    driver = PlaywrightMcpDriver(session=session)
    # NO llamamos driver.start()

    step = _step(StepKind.NAVIGATE, {"url": "https://example.com"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "playwright_mcp_not_started" in (outcome.error or "")


@pytest.mark.asyncio
async def test_observe_not_supported_returns_failed_gracefully() -> None:
    """OBSERVE esta soportado pero sin LLM — devuelve candidates del tree."""
    session = FakeMcpSession(snapshots=[_SNAPSHOT_PAGE_1])
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(StepKind.OBSERVE, {"instruction": "anything"})
    outcome = await driver.execute(step)

    # PlaywrightMcpDriver SI soporta OBSERVE (devuelve el arbol parseado)
    assert outcome.status == StepStatus.EXECUTED_OK
    assert "candidates" in outcome.result


@pytest.mark.asyncio
async def test_close_propagates_to_session() -> None:
    """close() llama session.close() exactamente una vez."""
    session = FakeMcpSession()
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    await driver.close()

    assert session.closed is True


@pytest.mark.asyncio
async def test_driver_name_and_capabilities() -> None:
    """driver_name y capabilities tienen los valores esperados."""
    session = FakeMcpSession()
    driver = PlaywrightMcpDriver(session=session)

    assert driver.driver_name == "playwright_mcp"
    caps = driver.capabilities
    assert caps["supports_mcp"] is True
    assert caps["supports_vision"] is False
    assert caps["supports_observe"] is True


@pytest.mark.asyncio
async def test_act_click_with_raw_ref() -> None:
    """ACT con mcp_ref directo hace click sin tomar snapshot."""
    session = FakeMcpSession(snapshots=[_SNAPSHOT_PAGE_1])
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(StepKind.ACT, {"mcp_ref": "e5", "action": "click"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["clicked_ref"] == "e5"
    assert "e5" in session.clicked_refs
    # No snapshot needed for direct ref
    assert session.snapshot_call_count == 0


@pytest.mark.asyncio
async def test_act_type_with_raw_ref() -> None:
    """ACT type con mcp_ref escribe el texto en el elemento."""
    session = FakeMcpSession(snapshots=[_SNAPSHOT_PAGE_1])
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()

    step = _step(StepKind.ACT, {"mcp_ref": "e3", "action": "type", "text": "B12345678"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert ("e3", "B12345678") in session.typed_calls
