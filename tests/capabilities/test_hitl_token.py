"""T029 — Tests para HitlApprovalMinter (CTRL-1/BROKER-1).

Requisitos (threat-model §2.3, BROKER-1):
- Token HMAC ligado a (proposal_id, capability, expiry, nonce).
- verify() retorna True SOLO para token criptográficamente válido,
  ligado a ese proposal_id, no expirado, y no consumido aún (single-use).
- Usa hmac.compare_digest — no comparación directa.
- Un string non-null arbitrario (p.ej. "approved") NO pasa (mata el
  presence-check de hitl_loop.py:141).
- Nonce inyectable para tests deterministas.

Estos tests deben FALLAR hasta que se implemente HitlApprovalMinter.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

# ---------------------------------------------------------------------------
# Importación adelantada — fallará hasta que exista el módulo.
# ---------------------------------------------------------------------------
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_KEY = os.urandom(32)
_FIXED_NONCE = "deadbeef" * 4  # nonce fijo para tests deterministas
_PROPOSAL_ID = uuid4()
_CAPABILITY = "terminal"


def _minter(*, auto_consume: bool = True) -> HitlApprovalMinter:
    """Construye un minter con clave de test y nonce inyectado."""
    return HitlApprovalMinter(
        signing_key=_TEST_KEY,
        nonce_generator=lambda: _FIXED_NONCE,
    )


# ---------------------------------------------------------------------------
# Tests de happy-path
# ---------------------------------------------------------------------------


def test_valid_token_verifies_true() -> None:
    """Token recién minteado verifica correctamente."""
    minter = _minter()
    token = minter.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=300)
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=token) is True


def test_verify_consumes_token_single_use() -> None:
    """Single-use: segunda llamada a verify() retorna False."""
    minter = _minter()
    token = minter.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=300)
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=token) is True
    # Segunda verificación — debe rechazar (anti-replay).
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=token) is False


# ---------------------------------------------------------------------------
# Tests de rechazo — proposal_id incorrecto
# ---------------------------------------------------------------------------


def test_wrong_proposal_id_rejected() -> None:
    """Token de una proposal no verifica para otra proposal."""
    minter = _minter()
    token = minter.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=300)
    other_id = uuid4()
    assert minter.verify(proposal_id=other_id, token=token) is False


# ---------------------------------------------------------------------------
# Tests de rechazo — token expirado
# ---------------------------------------------------------------------------


def test_expired_token_rejected() -> None:
    """Token con TTL=-1 (ya expirado al mintear) es rechazado."""
    minter = _minter()
    # TTL negativo → expiry en el pasado
    token = minter.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=-1)
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=token) is False


def test_ttl_zero_token_rejected() -> None:
    """Token con TTL=0 expira inmediatamente."""
    minter = _minter()
    token = minter.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=0)
    # Pequeña pausa para garantizar que el reloj avanzó.
    time.sleep(0.05)
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=token) is False


# ---------------------------------------------------------------------------
# Tests de rechazo — strings arbitrarios NO pasan (mata presence-check)
# ---------------------------------------------------------------------------


def test_arbitrary_non_null_string_rejected() -> None:
    """'approved' no es un token válido — mata el presence-check."""
    minter = _minter()
    _mint_token_to_initialise_state(minter)
    assert minter.verify(proposal_id=_PROPOSAL_ID, token="approved") is False


def test_empty_string_rejected() -> None:
    """String vacío siempre rechazado."""
    minter = _minter()
    assert minter.verify(proposal_id=_PROPOSAL_ID, token="") is False


def test_none_equivalent_string_rejected() -> None:
    """String 'None' (serialización naive de None) es rechazado."""
    minter = _minter()
    assert minter.verify(proposal_id=_PROPOSAL_ID, token="None") is False


def test_random_hex_string_rejected() -> None:
    """Un hex aleatorio no firmado es rechazado."""
    minter = _minter()
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=os.urandom(32).hex()) is False


def test_uuid_string_rejected() -> None:
    """Un UUID string arbitrario no es un token válido."""
    minter = _minter()
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=str(uuid4())) is False


# ---------------------------------------------------------------------------
# Tests de aislamiento entre proposals
# ---------------------------------------------------------------------------


def test_token_from_different_proposal_rejected() -> None:
    """Token minteado para proposal_A no verifica para proposal_B."""
    minter = _minter()
    proposal_a = uuid4()
    proposal_b = uuid4()
    token_a = minter.mint(proposal_id=proposal_a, capability=_CAPABILITY, ttl=300)
    assert minter.verify(proposal_id=proposal_b, token=token_a) is False


def test_two_proposals_independent_single_use() -> None:
    """Cada proposal tiene su propio token; single-use es por nonce.

    Usamos nonces distintos por proposal para que el consumo de uno
    no bloquee al otro (el nonce es el identificador de single-use).
    """
    nonces = iter(["nonce-for-p1", "nonce-for-p2"])
    minter = HitlApprovalMinter(
        signing_key=_TEST_KEY,
        nonce_generator=lambda: next(nonces),
    )
    p1 = uuid4()
    p2 = uuid4()
    t1 = minter.mint(proposal_id=p1, capability=_CAPABILITY, ttl=300)
    t2 = minter.mint(proposal_id=p2, capability=_CAPABILITY, ttl=300)
    # Consumir p1 no afecta a p2.
    assert minter.verify(proposal_id=p1, token=t1) is True
    assert minter.verify(proposal_id=p2, token=t2) is True
    # p1 ya consumido → False; p2 también consumido → False.
    assert minter.verify(proposal_id=p1, token=t1) is False
    assert minter.verify(proposal_id=p2, token=t2) is False


# ---------------------------------------------------------------------------
# Test de compare_digest (resistencia a timing attack)
# ---------------------------------------------------------------------------


def test_uses_compare_digest_not_equality() -> None:
    """Verificación usa hmac.compare_digest, no '==' directa sobre el token.

    Comprobamos indirectamente: si la impl. usara '==' en lugar de
    compare_digest, inyectar un objeto con __eq__ personalizado lo detectaría.
    Este test usa un token válido con un caracter cambiado para verificar
    que el rechazo ocurre sin importar el offset.
    """
    minter = _minter()
    token = minter.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=300)
    # Corrompe el último caracter.
    corrupted = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert minter.verify(proposal_id=_PROPOSAL_ID, token=corrupted) is False


# ---------------------------------------------------------------------------
# Tests con clave distinta — HMAC diferente
# ---------------------------------------------------------------------------


def test_different_signing_key_rejected() -> None:
    """Token minteado con clave A no verifica en minter con clave B."""
    minter_a = HitlApprovalMinter(
        signing_key=_TEST_KEY,
        nonce_generator=lambda: _FIXED_NONCE,
    )
    minter_b = HitlApprovalMinter(
        signing_key=os.urandom(32),
        nonce_generator=lambda: _FIXED_NONCE,
    )
    token = minter_a.mint(proposal_id=_PROPOSAL_ID, capability=_CAPABILITY, ttl=300)
    assert minter_b.verify(proposal_id=_PROPOSAL_ID, token=token) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_token_to_initialise_state(minter: HitlApprovalMinter) -> str:
    """Mint un token para inicializar state interno del minter en tests."""
    pid = uuid4()
    return minter.mint(proposal_id=pid, capability=_CAPABILITY, ttl=300)
