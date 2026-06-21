"""Unit tests del AgentBrowserDriver con FakeAgentBrowserCli.

Cubre los 4 contratos exigidos por el requisito:
  (a) navigate + snapshot devuelve arbol semantico filtrado.
  (b) OBSERVE: una accion grabada almacena role+name+description, NO @eN crudo.
  (c) Replay re-resuelve el @eN desde un snapshot fresco cuando la pagina cambia.
  (d) Self-healing path cuando el selector almacenado no se encuentra.

Constitution V: sin Chromium, sin binario Rust, sin red.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.domain.step import Step, StepKind, StepRisk, StepStatus
from hermes.browser.infrastructure.agent_browser_driver import (
    AgentBrowserDriver,
    _parse_candidates,
    _resolve_ref,
)
from hermes.browser.testing.fake_agent_browser_cli import FakeAgentBrowserCli

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


# Snapshot PAGE_1: refs @e1/@e3/@e5 — estado antes de la navegacion.
# Formato del accessibility tree de agent-browser (snapshot -i):
#   @eN [role attrs] "accessible name"
_SNAPSHOT_PAGE_1 = "\n".join([
    "Page: Formulario",
    "URL: https://example.com/form",
    "",
    '@e1 [heading] "Formulario 303"',
    '@e3 [input type="text"] "NIF"',
    '@e5 [button type="submit"] "Presentar definitivo"',
    '@e12 [link] "Inicio"',
])

# Snapshot PAGE_2: los MISMOS elementos pero con refs DISTINTOS.
# Simula el drift de refs que ocurre tras una navegacion o re-render.
_SNAPSHOT_PAGE_2 = "\n".join([
    "Page: Formulario (enviado)",
    "URL: https://example.com/form?submitted=1",
    "",
    '@e2 [heading] "Formulario 303"',          # mismo role+name, ref cambiado
    '@e21 [input type="text"] "NIF"',           # ref cambiado
    '@e99 [button type="submit"] "Presentar definitivo"',   # ref cambiado
    '@e44 [link] "Inicio"',                     # ref cambiado
])


# ---------------------------------------------------------------------------
# (a) navigate + snapshot devuelve arbol semantico
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_returns_current_url() -> None:
    """NAVIGATE devuelve la URL actual tras la navegacion."""
    cli = FakeAgentBrowserCli(
        current_urls=["about:blank", "https://example.com/form"],
        snapshots=[_SNAPSHOT_PAGE_1],
    )
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(StepKind.NAVIGATE, {"url": "https://example.com/form"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["url"] == "https://example.com/form"
    assert cli.navigated_urls == ["https://example.com/form"]


@pytest.mark.asyncio
async def test_take_dom_snapshot_returns_accessibility_tree() -> None:
    """take_dom_snapshot devuelve el accessibility tree como texto plano."""
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    snapshot = await driver.take_dom_snapshot()

    assert "@e5" in snapshot
    assert "Presentar definitivo" in snapshot
    assert "[button" in snapshot


@pytest.mark.asyncio
async def test_observe_returns_semantic_candidates_not_raw_refs() -> None:
    """OBSERVE produce candidates con value='@role=X @name=Y', NO @eN crudo."""
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(StepKind.OBSERVE, {"instruction": "botones del formulario"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    candidates = outcome.result["candidates"]
    assert len(candidates) >= 3

    button_candidate = next(
        c for c in candidates if "Presentar definitivo" in c["value"]
    )
    # El value almacena la identidad semantica durable, nunca el ref efimero.
    assert button_candidate["strategy"] == "accessibility_ref"
    assert button_candidate["value"] == "@role=button @name=Presentar definitivo"
    assert "@e" not in button_candidate["value"], (
        "El 'value' de un candidato NO debe contener el ref efimero @eN"
    )
    # El ref efimero SOLO debe estar en metadata
    assert button_candidate["metadata"]["ref"] == "e5"


# ---------------------------------------------------------------------------
# (b) Candidato grabado: almacena role+name (durable), no @eN crudo
# ---------------------------------------------------------------------------


def test_parse_candidates_extracts_semantic_value() -> None:
    """_parse_candidates extrae @role+@name como identidad durable."""
    candidates = _parse_candidates(_SNAPSHOT_PAGE_1)

    values = [c["value"] for c in candidates]
    assert "@role=button @name=Presentar definitivo" in values
    assert "@role=link @name=Inicio" in values
    assert "@role=input @name=NIF" in values

    # Ninguna value debe contener un ref efimero
    for c in candidates:
        assert "@e" not in c["value"], (
            f"El campo 'value' del candidato contiene ref efimero: {c['value']!r}"
        )


def test_parse_candidates_confidence_unique_name_is_high() -> None:
    """Elementos con nombre unico reciben confidence 0.9; duplicados 0.5."""
    snapshot = "\n".join([
        '@e1 [button] "Enviar"',
        '@e2 [button] "Enviar"',   # nombre duplicado
        '@e3 [link] "Home"',       # nombre unico
    ])
    candidates = _parse_candidates(snapshot)
    by_ref = {c["metadata"]["ref"]: c for c in candidates}

    assert by_ref["e1"]["confidence"] == 0.5
    assert by_ref["e2"]["confidence"] == 0.5
    assert by_ref["e3"]["confidence"] == 0.9


def test_parse_candidates_role_attrs_stripped() -> None:
    """Los atributos inline del role (type="email") se descartan en el value."""
    snapshot = '@e3 [input type="email"] "Email address"'
    candidates = _parse_candidates(snapshot)

    assert len(candidates) == 1
    assert candidates[0]["value"] == "@role=input @name=Email address"


# ---------------------------------------------------------------------------
# (c) Replay re-resuelve el @eN desde snapshot fresco cuando la pagina cambia
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
    (durable), NO @e5. Al replay se toma _SNAPSHOT_PAGE_2 donde el mismo
    boton tiene ref=e99, y el driver lo encuentra correctamente.
    """
    old_ref = _resolve_ref(_SNAPSHOT_PAGE_1, role="button", name="Presentar definitivo")
    new_ref = _resolve_ref(_SNAPSHOT_PAGE_2, role="button", name="Presentar definitivo")

    assert old_ref == "e5"
    assert new_ref == "e99"
    assert old_ref != new_ref, "El ref debe cambiar — el test simula drift de la pagina"


@pytest.mark.asyncio
async def test_act_with_ab_identity_resolves_fresh_ref_on_changed_page() -> None:
    """ACT con ab_identity toma un snapshot fresco y re-resuelve el @eN actual.

    Simulamos que entre el recording y el replay la pagina cambio:
      - Al grabar, el boton era @e5 (en _SNAPSHOT_PAGE_1).
      - Al reproducir, el driver recibe ab_identity={role, name} y toma
        _SNAPSHOT_PAGE_2 donde el mismo boton tiene @e99.
    El driver debe hacer click en @e99, no en @e5.
    """
    cli = FakeAgentBrowserCli(
        snapshots=[_SNAPSHOT_PAGE_2],  # pagina cambiada en replay
        current_urls=["https://example.com/form?submitted=1"],
    )
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(
        StepKind.ACT,
        {
            "ab_identity": {"role": "button", "name": "Presentar definitivo"},
            "action": "click",
        },
    )
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["clicked_ref"] == "e99", (
        "El driver debe haber re-resuelto el ref desde el snapshot fresco"
    )
    # Verificar que se hizo snapshot (re-resolution) y click
    assert cli.snapshot_call_count >= 1
    # El CLI recibe el ref con prefijo @
    assert "@e99" in cli.clicked_refs


# ---------------------------------------------------------------------------
# (d) Self-healing: selector no encontrado devuelve failed
# ---------------------------------------------------------------------------


def test_resolve_ref_returns_none_when_not_found() -> None:
    """_resolve_ref devuelve None si el role+name no existe en el snapshot."""
    ref = _resolve_ref(_SNAPSHOT_PAGE_1, role="button", name="Boton inexistente")
    assert ref is None


@pytest.mark.asyncio
async def test_act_with_missing_identity_returns_failed_outcome() -> None:
    """ACT con ab_identity que no se resuelve devuelve StepOutcome.failed.

    El driver sigue Constitution IV: fail-closed, sin excepcion al consumer.
    Upstream (SelfHealer) puede interpretar el error y pedir OperatorIntervention.
    """
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(
        StepKind.ACT,
        {
            "ab_identity": {"role": "button", "name": "Boton que ya no existe"},
            "action": "click",
        },
    )
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "ab_ref_not_found" in (outcome.error or "")
    assert cli.clicked_refs == []


@pytest.mark.asyncio
async def test_act_without_ref_or_identity_returns_failed() -> None:
    """ACT sin ab_ref ni ab_identity devuelve error descriptivo."""
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(StepKind.ACT, {"action": "click"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "act_requires_ab_ref_or_ab_identity" in (outcome.error or "")


# ---------------------------------------------------------------------------
# Otros comportamientos del driver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_before_start_returns_failed() -> None:
    """execute() sin llamar start() devuelve failed, no excepcion."""
    cli = FakeAgentBrowserCli()
    driver = AgentBrowserDriver(cli=cli)
    # NO llamamos driver.start()

    step = _step(StepKind.NAVIGATE, {"url": "https://example.com"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "agent_browser_not_started" in (outcome.error or "")


@pytest.mark.asyncio
async def test_driver_name_and_capabilities() -> None:
    """driver_name y capabilities tienen los valores esperados."""
    cli = FakeAgentBrowserCli()
    driver = AgentBrowserDriver(cli=cli)

    assert driver.driver_name == "agent_browser"
    caps = driver.capabilities
    assert caps["supports_observe"] is True
    assert caps["token_efficient_snapshots"] is True
    assert caps["experimental"] is True
    assert caps["supports_vision"] is False


@pytest.mark.asyncio
async def test_act_click_with_raw_ref() -> None:
    """ACT con ab_ref directo hace click sin tomar snapshot."""
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(StepKind.ACT, {"ab_ref": "e5", "action": "click"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["clicked_ref"] == "e5"
    # El CLI recibe el ref con prefijo @
    assert "@e5" in cli.clicked_refs
    # No snapshot needed for direct ref
    assert cli.snapshot_call_count == 0


@pytest.mark.asyncio
async def test_act_type_with_raw_ref() -> None:
    """ACT type con ab_ref escribe el texto en el elemento."""
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(StepKind.ACT, {"ab_ref": "e3", "action": "type", "text": "B12345678"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert ("@e3", "B12345678") in cli.typed_calls


@pytest.mark.asyncio
async def test_close_propagates_to_cli() -> None:
    """close() llama cli.close() exactamente una vez."""
    cli = FakeAgentBrowserCli()
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    await driver.close()

    assert cli.closed is True


@pytest.mark.asyncio
async def test_observe_returns_candidates_as_semantic_values() -> None:
    """OBSERVE esta soportado y devuelve el arbol parseado como candidates."""
    cli = FakeAgentBrowserCli(snapshots=[_SNAPSHOT_PAGE_1])
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

    step = _step(StepKind.OBSERVE, {"instruction": "anything"})
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_OK
    assert "candidates" in outcome.result
    assert len(outcome.result["candidates"]) >= 3
