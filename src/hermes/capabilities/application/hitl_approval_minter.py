"""T037 — HitlApprovalMinter (CTRL-1/BROKER-1/TOP-1).

Mint y verificación criptográfica del token HITL.

Diseño de seguridad (threat-model §2.3/BROKER-1):
  - Token = HMAC-SHA256(key, "{proposal_id}:{capability}:{expiry_unix}:{nonce}")
    codificado en hex.
  - `expiry_unix` es segundos desde epoch UTC (entero).
  - Nonce aleatorio por-proposal (os.urandom(16).hex() por defecto) → distintos
    tokens aunque se mintee dos veces para el mismo proposal_id.
  - Payload serializado: `"{proposal_id}|{capability}|{expiry_unix}|{nonce}"`.
    Separador `|` para evitar confusión con valores que contengan `:`.
  - El token opaco tiene la forma:
      `{payload_hex}.{hmac_hex}`
    donde `payload_hex = payload_bytes.hex()`.
    Así el verificador puede reconstruir el payload sin almacenamiento externo.

Single-use (anti-replay):
  - El conjunto `_consumed_nonces` registra nonces consumidos.
  - `verify()` marca el nonce como consumido ANTES de retornar True.
    Si el nonce ya está en el conjunto → False (fail-closed).
  - El estado de consumo es in-memory para los tests unitarios;
    en producción el SqliteApprovalGate almacena `consumed_at` en la BD.

compare_digest:
  - Se usa `hmac.compare_digest` para la comparación final.
    Resistente a timing attacks (CWE-307).

Clave de firma:
  - Inyectada en el constructor (en producción: `load_signing_key()` de
    `hermes.runtime.audit_signing_key`).
  - La clave NUNCA aparece en logs ni en mensajes de error.

Nonce inyectable:
  - `nonce_generator: Callable[[], str]` permite inyectar un generador
    determinista en tests. Por defecto usa `os.urandom(16).hex()`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable
from uuid import UUID

_MIN_SIGNING_KEY_BYTES: int = 32
"""Longitud mínima de la clave de firma HMAC (bytes)."""


class InvalidHitlToken(ValueError):
    """El token HITL es inválido, expirado o ya consumido."""


class HitlApprovalMinter:
    """Mint y verifica tokens HMAC de aprobación HITL.

    Args:
        signing_key:     Clave HMAC de al menos 32 bytes (sellada desde LUKS/TPM2).
        nonce_generator: Callable sin args que genera un nonce string único
                         por mint. Inyectable para tests deterministas.
    """

    def __init__(
        self,
        *,
        signing_key: bytes,
        nonce_generator: Callable[[], str] | None = None,
    ) -> None:
        if len(signing_key) < _MIN_SIGNING_KEY_BYTES:
            raise ValueError("signing_key debe tener al menos 32 bytes")
        self._key = signing_key
        self._nonce_gen = nonce_generator or _default_nonce_generator
        # Registro de nonces consumidos (anti-replay, single-use).
        self._consumed_nonces: set[str] = set()

    def mint(
        self,
        *,
        proposal_id: UUID,
        capability: str,
        ttl: int,
    ) -> str:
        """Genera un token HMAC opaco ligado a (proposal_id, capability, expiry, nonce).

        Args:
            proposal_id: UUID de la propuesta (ligado criptográficamente).
            capability:  Capability autorizada (p.ej. "terminal").
            ttl:         Tiempo de vida en segundos desde ahora.
                         TTL ≤ 0 genera un token ya expirado (fail-closed en verify).

        Returns:
            Token opaco `{payload_hex}.{hmac_hex}` listo para enviar al broker.
        """
        expiry_unix = int(time.time()) + ttl
        nonce = self._nonce_gen()
        payload = _build_payload(proposal_id, capability, expiry_unix, nonce)
        mac = _compute_hmac(self._key, payload)
        return f"{payload.encode().hex()}.{mac}"

    def verify(self, *, proposal_id: UUID, token: str) -> bool:  # noqa: PLR0911
        """Verifica criptográficamente el token contra esta propuesta.

        Reglas fail-closed (todas deben pasar — cualquier fallo retorna False):
          1. El token tiene la estructura `{payload_hex}.{hmac_hex}`.
          2. El HMAC es válido (compare_digest).
          3. El payload contiene el proposal_id correcto.
          4. El token no ha expirado (expiry_unix > now).
          5. El nonce no fue consumido previamente (single-use).

        Marca el nonce como consumido si y SOLO si todas las verificaciones pasan.

        Returns:
            True si el token es válido y no había sido consumido.
            False en cualquier otro caso (fail-closed).
        """
        parsed = _parse_token(token)
        if parsed is None:
            return False

        payload_str, mac_received = parsed

        # 1. Verificación HMAC (compare_digest, resistente a timing).
        mac_expected = _compute_hmac(self._key, payload_str)
        if not hmac.compare_digest(mac_expected, mac_received):
            return False

        # 2. Parsear payload.
        fields = _parse_payload(payload_str)
        if fields is None:
            return False

        token_proposal_id, _capability, expiry_unix, nonce = fields

        # 3. proposal_id correcto.
        if token_proposal_id != str(proposal_id):
            return False

        # 4. Expiración.
        if int(time.time()) >= expiry_unix:
            return False

        # 5. Single-use: nonce no consumido.
        if nonce in self._consumed_nonces:
            return False

        # Todas las verificaciones pasan → marcar consumido.
        self._consumed_nonces.add(nonce)
        return True


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _default_nonce_generator() -> str:
    return os.urandom(16).hex()


def _build_payload(
    proposal_id: UUID,
    capability: str,
    expiry_unix: int,
    nonce: str,
) -> str:
    """Construye el string payload canónico para HMAC y transporte."""
    return f"{proposal_id}|{capability}|{expiry_unix}|{nonce}"


def _compute_hmac(key: bytes, payload: str) -> str:
    """HMAC-SHA256 del payload; devuelve hex."""
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _parse_token(token: str) -> tuple[str, str] | None:
    """Divide el token en (payload_str, mac_hex).

    Retorna None si el formato es incorrecto (fail-closed).
    """
    if not token or "." not in token:
        return None
    # Separar por el ÚLTIMO punto para que el payload pueda contener puntos.
    dot_idx = token.rfind(".")
    payload_hex = token[:dot_idx]
    mac_hex = token[dot_idx + 1 :]
    if not payload_hex or not mac_hex:
        return None
    try:
        payload_str = bytes.fromhex(payload_hex).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return payload_str, mac_hex


def _parse_payload(payload: str) -> tuple[str, str, int, str] | None:
    """Parsea el payload en (proposal_id, capability, expiry_unix, nonce).

    Retorna None si el formato es incorrecto (fail-closed).
    """
    parts = payload.split("|")
    if len(parts) != 4:  # noqa: PLR2004
        return None
    proposal_id_str, capability, expiry_str, nonce = parts
    try:
        expiry_unix = int(expiry_str)
    except ValueError:
        return None
    return proposal_id_str, capability, expiry_unix, nonce
