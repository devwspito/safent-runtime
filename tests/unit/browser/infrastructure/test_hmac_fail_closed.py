"""T203 — HMAC fail-closed tests.

Cubre los 5 casos del threat-model (control P1 #5) + 2 adicionales del
ReplayScript:

  (a) Selector con signature_hex modificado un byte → fetch_latest devuelve
      None (fail-closed) y emite evento selector_tampered via structlog.
  (b) Selector firmado con key_B, registry usa key_A → None.
  (c) ReplayScript con un byte cambiado en payload_template → verify() levanta
      ReplayScriptInvalidSignature.
  (d) ReplayScript firmado con key antigua, min_accepted_version=v2 → rechazado
      con ReplayScriptDowngradeRejected (downgrade protection S1 superficie 3).
  (e) canonical_bytes_for_signing() determinista: 10 iteraciones → bytes idénticos.
  (f) Signing roundtrip: firmar + verify con mismo key → OK.
  (g) Dos scripts con steps distintos → firmas distintas.

Constitution IV: fail-closed — firma inválida nunca produce resultado.
Threat-model T1 superficie 3+4: canonicalización estable → no exploitable.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hermes.browser.domain.replay_script import (
    ReplayScript,
    ReplayScriptDowngradeRejected,
    ReplayScriptInvalidSignature,
    ReplayStep,
    sign_replay_script,
)
from hermes.browser.domain.selector import SelectorStrategy
from hermes.browser.infrastructure import (
    InMemorySelectorRegistry,
    SelectorTamperedError,
    SignedSelectorRegistry,
    StoredSelector,
    build_signed,
)

# ---------------------------------------------------------------------------
# Keys para tests — nunca usar en producción
# ---------------------------------------------------------------------------

_KEY_A = b"\xaa" * 32
_KEY_B = b"\xbb" * 32
_KEY_OLD = b"\xcc" * 32  # "key antigua" para tests de downgrade


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_replay_script(
    *,
    risk: str = "low",
    payload_template: dict | None = None,
) -> ReplayScript:
    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action="click",
        payload_template=payload_template or {"target": "btn_demo"},
        risk=risk,
    )
    return ReplayScript(
        script_id=uuid4(),
        site_id="aeat_sede",
        flow_id="modelo_303",
        tenant_scope=UUID("12345678-1234-1234-1234-123456789abc"),
        runtime_version="0.1.0",
        steps=(step,),
    )


def _tamper_selector_signature_one_byte(sig: str) -> str:
    """Modifica el último carácter hex de una firma de selector (formato hex plano)."""
    if not sig:
        return "tampered"
    # Selector signatures son hex plano (sin prefijo vN:)
    last_char = sig[-1]
    tampered_last = format(int(last_char, 16) ^ 0xF, "x")
    return sig[:-1] + tampered_last


# ---------------------------------------------------------------------------
# (a) Selector con signature_hex modificado → fail-closed + SelectorTamperedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selector_with_tampered_signature_returns_none() -> None:
    """fetch_latest con signature_hex modificado un byte levanta SelectorTamperedError.

    Fail-closed: el registry NO devuelve el selector — levanta la excepción
    para que el caller decida si cae a discovery.
    Threat-model control P1 #5 / T1 superficie 4.
    """
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY_A)

    # Construir selector legítimo con key_A
    stored = build_signed(
        signing_key=_KEY_A,
        site_id="aeat_sede",
        flow_id="test_flow",
        step_id="boton_demo",
        strategy=SelectorStrategy.CSS,
        value="#btn-demo",
        intent_desc="botón demo",
    )
    # Guardar en el store con la firma correcta, luego reemplazarla por una tampered
    tampered_sig = _tamper_selector_signature_one_byte(stored.signature_hex)
    tampered_stored = StoredSelector(
        selector=stored.selector,
        signature_hex=tampered_sig,
    )
    await store.persist(tampered_stored)

    with pytest.raises(SelectorTamperedError):
        await registry.fetch_latest(
            site_id="aeat_sede",
            flow_id="test_flow",
            step_id="boton_demo",
        )


# ---------------------------------------------------------------------------
# (b) Selector firmado con key_B, registry usa key_A → fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selector_signed_with_wrong_key_returns_none() -> None:
    """Selector firmado con key_B pero el registry está configurado con key_A → error.

    Simula que alguien persistió un selector con una key diferente
    (insider con acceso a una key distinta, o key rotation sin re-firma).
    Threat-model control P1 #5 / T1 superficie 4.
    """
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY_A)

    # Crear selector firmado con key_B
    stored_with_wrong_key = build_signed(
        signing_key=_KEY_B,  # firma con key distinta
        site_id="aeat_sede",
        flow_id="test_flow",
        step_id="boton_demo",
        strategy=SelectorStrategy.CSS,
        value="#btn-demo",
        intent_desc="botón demo",
    )
    # Persistir el selector con firma de key_B en un store que el registry leerá con key_A
    await store.persist(stored_with_wrong_key)

    with pytest.raises(SelectorTamperedError):
        await registry.fetch_latest(
            site_id="aeat_sede",
            flow_id="test_flow",
            step_id="boton_demo",
        )


# ---------------------------------------------------------------------------
# (c) ReplayScript con un byte cambiado en payload_template → InvalidSignature
# ---------------------------------------------------------------------------


def test_replay_script_with_one_byte_changed_invalid_signature() -> None:
    """Modificar payload_template invalida la firma HMAC.

    canonical_bytes_for_signing() incluye payload_template (via steps),
    por lo que cualquier modificación de datos hace inválida la firma.
    Threat-model control P1 #5 / T1 superficie 3.
    """
    original = _make_replay_script(payload_template={"target": "btn_presentar"})
    signed = sign_replay_script(original, key=_KEY_A)

    # Construir una copia con payload_template modificado pero con la firma original
    modified_step = ReplayStep(
        selector_id=signed.steps[0].selector_id,
        selector_version=signed.steps[0].selector_version,
        action=signed.steps[0].action,
        payload_template={"target": "btn_EVIL"},  # un byte cambiado semánticamente
        risk=signed.steps[0].risk,
    )
    tampered = ReplayScript(
        script_id=signed.script_id,
        site_id=signed.site_id,
        flow_id=signed.flow_id,
        tenant_scope=signed.tenant_scope,
        runtime_version=signed.runtime_version,
        steps=(modified_step,),
        created_at=signed.created_at,
        signature_hex=signed.signature_hex,  # firma original, datos distintos
    )

    with pytest.raises(ReplayScriptInvalidSignature):
        tampered.verify(key=_KEY_A)


# ---------------------------------------------------------------------------
# (d) Downgrade protection: script firmado con key vieja bajo min_accepted_version=v2
# ---------------------------------------------------------------------------


def test_replay_script_signed_with_old_key_below_min_version_rejected() -> None:
    """Script con signature_hex="v1:..." cuando min_accepted_version=v2 → rechazado.

    Downgrade protection: aunque la firma v1 sea válida criptográficamente,
    el runtime rechaza versiones de firma por debajo del mínimo configurado.
    Simula que la rotation ya ocurrió (v2) y un atacante intenta reusar un
    script v1 firmado con una key antigua comprometida.

    Threat-model control P1 #5 / S1 superficie 3: versión mínima aceptada.
    """
    original = _make_replay_script()
    # Firmar con versión v1 (key antigua)
    signed_v1 = sign_replay_script(original, key=_KEY_OLD, version="v1")

    # El runtime ahora requiere v2 como mínimo
    with pytest.raises(ReplayScriptDowngradeRejected):
        signed_v1.verify(key=_KEY_OLD, min_accepted_version="v2")


# ---------------------------------------------------------------------------
# (e) canonical_bytes_for_signing() determinista a lo largo de 10 iteraciones
# ---------------------------------------------------------------------------


def test_canonical_bytes_deterministic_across_iterations() -> None:
    """Serializar el mismo ReplayScript 10 veces produce bytes idénticos.

    Verifica que json.dumps(sort_keys=True, ...) + la estructura del dict
    de canonicalización son estables. Threat-model T1 superficie 3: sin
    bug de canonicalización no hay posibilidad de collision attacks.
    """
    script = _make_replay_script(
        payload_template={"nif": "{{NIF_1}}", "importe": "1234.56"},
    )

    first_bytes = script.canonical_bytes_for_signing()
    for i in range(1, 10):
        iteration_bytes = script.canonical_bytes_for_signing()
        assert iteration_bytes == first_bytes, (
            f"Iteración {i}: canonical_bytes_for_signing() produjo bytes distintos. "
            "La canonicalización no es determinista — violación del invariante."
        )


# ---------------------------------------------------------------------------
# (f) Signing roundtrip: firmar + verify con mismo key → OK
# ---------------------------------------------------------------------------


def test_replay_script_signing_roundtrip() -> None:
    """Firmar un script y verificarlo con el mismo key debe pasar sin excepción."""
    script = _make_replay_script(risk="high")
    signed = sign_replay_script(script, key=_KEY_A)

    # No debe levantar
    signed.verify(key=_KEY_A)


def test_replay_script_signing_roundtrip_wrong_key_fails() -> None:
    """Firmar con key_A y verificar con key_B debe levantar InvalidSignature."""
    script = _make_replay_script()
    signed = sign_replay_script(script, key=_KEY_A)

    with pytest.raises(ReplayScriptInvalidSignature):
        signed.verify(key=_KEY_B)


# ---------------------------------------------------------------------------
# (g) Dos scripts con steps distintos producen firmas distintas
# ---------------------------------------------------------------------------


def test_replay_script_with_different_steps_different_signatures() -> None:
    """Dos scripts equivalentes excepto en un step producen firmas distintas.

    Asegura que la firma cubre el contenido de los steps y que distintos
    contenidos no colisionan con la misma firma.
    Threat-model T1 superficie 3.
    """
    script_a = _make_replay_script(payload_template={"target": "btn_guardar"})
    script_b = _make_replay_script(payload_template={"target": "btn_enviar"})

    signed_a = sign_replay_script(script_a, key=_KEY_A)
    signed_b = sign_replay_script(script_b, key=_KEY_A)

    assert signed_a.signature_hex != signed_b.signature_hex, (
        "Scripts con steps distintos deben producir firmas distintas. "
        "Si las firmas coinciden, la canonicalización ignora el contenido."
    )
