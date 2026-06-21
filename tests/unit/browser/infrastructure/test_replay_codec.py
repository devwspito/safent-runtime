"""T501 — Tests replay_codec: canonicalize + HMAC.

Cubre los 5 contratos del threat-model T1 superficie 3:
  1. Roundtrip canonical bytes: 10 re-serializaciones produce bytes idénticos.
  2. sign_replay → signature_hex con prefijo "v1:" + hex válido.
  3. verify_replay con mismo key → no levanta.
  4. verify_replay con script modificado → ReplayScriptInvalidSignature.
  5. Downgrade attack: script v1 + min_accepted_version="v2" → ReplayScriptDowngradeRejected.

Constitución V: sin Chromium, sin red, sin DB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.domain.replay_script import (
    ReplayScript,
    ReplayScriptDowngradeRejected,
    ReplayScriptInvalidSignature,
    ReplayStep,
)
from hermes.browser.infrastructure.replay_codec import (
    canonical_bytes_for_signing,
    sign_replay,
    verify_replay,
)

_KEY = b"\xde\xad" * 16  # 32 bytes test key


def _make_script(*, risk: str = "low") -> ReplayScript:
    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action="click",
        payload_template={"click_selector": "#btn-presentar"},
        risk=risk,
    )
    return ReplayScript(
        script_id=uuid4(),
        site_id="aeat_sede",
        flow_id="modelo_303",
        tenant_scope=uuid4(),
        runtime_version="0.2.1",
        steps=(step,),
    )


# ---------------------------------------------------------------------------
# Test 1: canonical bytes determinista 10 iteraciones
# ---------------------------------------------------------------------------


def test_canonical_bytes_deterministic_10_iterations() -> None:
    """10 re-serializaciones del mismo script producen bytes idénticos.

    Verifica que sort_keys=True + separators fijos + encode("utf-8") no
    introducen variabilidad entre llamadas.
    Threat-model T1 superficie 3: sin bug de canonicalización.
    """
    script = _make_script()
    first = canonical_bytes_for_signing(script)

    for i in range(1, 10):
        result = canonical_bytes_for_signing(script)
        assert result == first, (
            f"Iteración {i}: canonical_bytes_for_signing() varió. "
            "La canonicalización no es determinista."
        )


# ---------------------------------------------------------------------------
# Test 2: signature_hex tiene prefijo "v1:" + contenido hex válido
# ---------------------------------------------------------------------------


def test_sign_replay_produces_v1_prefixed_hex_signature() -> None:
    """sign_replay devuelve script con signature_hex="v1:{hex}".

    El prefijo "v1:" identifica la versión de la key; el resto es hex
    ASCII de 64 caracteres (SHA-256).
    """
    script = _make_script()
    signed = sign_replay(script, key=_KEY)

    assert signed.signature_hex.startswith("v1:"), (
        f"signature_hex debe comenzar con 'v1:'. Got: {signed.signature_hex!r}"
    )
    _, hex_part = signed.signature_hex.split(":", 1)
    assert len(hex_part) == 64, (
        f"HMAC-SHA256 hex debe ser 64 caracteres. Got len={len(hex_part)}"
    )
    # Verify hex is valid ASCII hex
    assert all(c in "0123456789abcdef" for c in hex_part), (
        f"signature_hex contiene caracteres no-hex: {hex_part!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: verify_replay con mismo key → no levanta
# ---------------------------------------------------------------------------


def test_verify_replay_happy_path_does_not_raise() -> None:
    """sign_replay + verify_replay con misma key no levanta ninguna excepción."""
    script = _make_script()
    signed = sign_replay(script, key=_KEY)

    # Must not raise
    verify_replay(signed, key=_KEY)


# ---------------------------------------------------------------------------
# Test 4: verify_replay script modificado → ReplayScriptInvalidSignature
# ---------------------------------------------------------------------------


def test_verify_replay_modified_script_raises_invalid_signature() -> None:
    """Modificar un campo del script invalida la firma HMAC.

    El payload_template está incluido en la canonicalización —
    cualquier cambio debe romper la firma.
    """
    script = _make_script()
    signed = sign_replay(script, key=_KEY)

    # Modify one step's payload while keeping the original signature
    original_step = signed.steps[0]
    tampered_step = ReplayStep(
        selector_id=original_step.selector_id,
        selector_version=original_step.selector_version,
        action=original_step.action,
        payload_template={"click_selector": "#btn-EVIL"},  # changed!
        risk=original_step.risk,
    )
    tampered = ReplayScript(
        script_id=signed.script_id,
        site_id=signed.site_id,
        flow_id=signed.flow_id,
        tenant_scope=signed.tenant_scope,
        runtime_version=signed.runtime_version,
        steps=(tampered_step,),
        created_at=signed.created_at,
        signature_hex=signed.signature_hex,  # original signature, tampered data
    )

    with pytest.raises(ReplayScriptInvalidSignature):
        verify_replay(tampered, key=_KEY)


# ---------------------------------------------------------------------------
# Test 5: downgrade attack → ReplayScriptDowngradeRejected
# ---------------------------------------------------------------------------


def test_verify_replay_downgrade_attack_rejected() -> None:
    """Script firmado con v1 rechazado cuando min_accepted_version="v2".

    Simula key rotation: la vertical actualiza min_accepted_version a "v2".
    Un atacante con la key v1 (comprometida) no puede reusar scripts viejos.
    Threat-model S1 superficie 3: downgrade protection.
    """
    script = _make_script()
    signed_v1 = sign_replay(script, key=_KEY, version="v1")

    with pytest.raises(ReplayScriptDowngradeRejected):
        verify_replay(signed_v1, key=_KEY, min_accepted_version="v2")
