"""SignedSelectorRegistry: SelectorRegistry con HMAC SHA-256.

Anti-tampering a nivel DB. Si alguien hace UPDATE manual sobre la columna
`selector_json` saltandose el repository, la firma deja de validar y el
runtime descarta el selector (fail-closed).

Versiones de firma:
  v2: payload con `author` (T604). Cambiar `author` rompe la firma.
      Formato: "v2:<hex64>"

Politica de verificacion — PINADA a v2, fail-closed:
  - Solo firmas con prefijo "v2:" son aceptadas como validas.
  - Firmas con prefijo "v1:", sin prefijo (hex plano), o cualquier otro
    formato son RECHAZADAS (retornan False / levantan SelectorTamperedError).
  - Razon: v1 omite el campo `author` del payload HMAC, permitiendo
    forjar la atribucion del selector sin invalidar la firma. Aceptar v1
    es equivalente a no verificar la autoria.
  - No hay ventana de migracion v1: todos los selectores persistidos por
    el runtime ya usan v2 (sign_selector siempre produce "v2:<hex64>").
    Un selector con firma v1 es un indicador de tamper o de escritura
    directa en DB que elude el registry, lo que es exactamente el ataque
    que esta clase debe detectar.

La firma se construye sobre los campos:
  v2: (selector_id, site_id, flow_id, step_id, strategy, value, version,
       tenant_scope, author)

usando una `selector_signing_key: bytes` que la composition root entrega.
Recomendado: derivar de la master_key del KMS con HKDF
(info = b"hermes.browser.selector").

Este modulo NO toca Postgres directamente — implementa el algoritmo de
firma y el `Protocol` SelectorRegistry sobre un `SelectorStore` que cada
vertical implementa (Postgres / SQLite / etc.). El test usa
`InMemorySelectorRegistry` que es la implementacion mas simple.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from hermes.browser.domain.selector import Selector, SelectorAuthor, SelectorStrategy

logger = logging.getLogger(__name__)

_SIG_VERSION = "v2"
_V2_PREFIX = f"{_SIG_VERSION}:"


class SelectorTamperedError(RuntimeError):
    """Firma HMAC no valida -> registro tampered o key incorrecta."""


def _payload_bytes_v2(selector: Selector) -> bytes:
    """Payload v2: incluye author. Cambiar author rompe la firma (T604)."""
    parts: list[str] = [
        str(selector.selector_id),
        selector.site_id,
        selector.flow_id,
        selector.step_id,
        str(selector.strategy.value),
        selector.value,
        str(selector.version),
        str(selector.tenant_scope) if selector.tenant_scope else "",
        selector.author.value,  # T604: author incluido en HMAC
    ]
    return "\x1f".join(parts).encode("utf-8")



def _hmac_hex(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def sign_selector(selector: Selector, *, key: bytes) -> str:
    """Devuelve la firma HMAC-SHA256 v2 del selector, prefijada con 'v2:'."""
    if not key:
        raise ValueError("signing key cannot be empty")
    return f"{_SIG_VERSION}:{_hmac_hex(key, _payload_bytes_v2(selector))}"


def verify_selector_signature(
    selector: Selector,
    signature_hex: str,
    *,
    key: bytes,
) -> bool:
    """Verifica firma constant-time — PINADA a v2, fail-closed.

    Retorna True si y solo si:
      - La firma comienza con "v2:" Y
      - El HMAC-SHA256 del payload v2 es correcto.

    Retorna False en cualquier otro caso:
      - signature_hex vacio.
      - Firma v1 (prefijo "v1:") — RECHAZADA: v1 omite `author`, forjable.
      - Firma sin prefijo (hex plano legacy) — RECHAZADA: misma razon.
      - Cualquier otro prefijo desconocido — RECHAZADO.
      - HMAC v2 incorrecto.

    Fail-closed (Constitucion IV + Principio 0): ante cualquier duda, False.
    """
    if not signature_hex:
        return False

    if not signature_hex.startswith(_V2_PREFIX):
        logger.warning(
            "hermes.browser.selector.non_v2_signature_rejected",
            extra={
                "selector_id": str(selector.selector_id),
                "prefix": signature_hex[:4] if signature_hex else "",
                "note": (
                    "Only v2 signatures are accepted. "
                    "v1 and unprefixed signatures are forgeable (author field missing)."
                ),
            },
        )
        return False

    return _verify_v2(selector, signature_hex, key=key)


def _verify_v2(selector: Selector, signature_hex: str, *, key: bytes) -> bool:
    raw_hex = signature_hex[len(_V2_PREFIX):]
    expected = _hmac_hex(key, _payload_bytes_v2(selector))
    return hmac.compare_digest(expected, raw_hex)


# ---------------------------------------------------------------------------
# Storage backend protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredSelector:
    selector: Selector
    signature_hex: str


class SelectorStore(Protocol):
    """Persistencia raw del selector. La vertical implementa Postgres / etc."""

    async def fetch_latest(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None,
    ) -> StoredSelector | None: ...

    async def history(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None,
    ) -> Sequence[StoredSelector]: ...

    async def persist(self, stored: StoredSelector) -> None: ...

    async def mark_deprecated_by_id(self, selector_id: UUID, *, reason: str) -> None: ...

    async def touch_ok_by_id(self, selector_id: UUID) -> None: ...


# ---------------------------------------------------------------------------
# SignedSelectorRegistry: wrap a SelectorStore con HMAC verification.
# ---------------------------------------------------------------------------


class SignedSelectorRegistry:
    """Implementa `SelectorRegistry` Protocol verificando HMAC siempre.

    Fail-closed: si la firma no valida (selector tampered o key
    incorrecta), levanta `SelectorTamperedError` en lugar de devolver
    el selector. El consumidor decide si reintenta con fresh discovery.
    """

    def __init__(self, *, store: SelectorStore, signing_key: bytes) -> None:
        if not signing_key:
            raise ValueError("signing_key cannot be empty")
        self._store = store
        self._key = signing_key

    async def fetch_latest(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None = None,
    ) -> Selector | None:
        stored = await self._store.fetch_latest(
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            tenant_scope=tenant_scope,
        )
        if stored is None:
            return None
        if not verify_selector_signature(
            stored.selector, stored.signature_hex, key=self._key
        ):
            raise SelectorTamperedError(
                f"Selector {stored.selector.selector_id} firma HMAC invalida"
            )
        if stored.selector.deprecated_at is not None:
            return None
        return stored.selector

    async def history(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        tenant_scope: UUID | None = None,
    ) -> Sequence[Selector]:
        rows = await self._store.history(
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            tenant_scope=tenant_scope,
        )
        result: list[Selector] = []
        for stored in rows:
            if not verify_selector_signature(
                stored.selector, stored.signature_hex, key=self._key
            ):
                raise SelectorTamperedError(
                    f"Selector {stored.selector.selector_id} firma HMAC invalida"
                )
            result.append(stored.selector)
        return result

    async def persist(self, selector: Selector) -> None:
        # Marca el previo activo como deprecated antes de guardar el nuevo.
        previous = await self._store.fetch_latest(
            site_id=selector.site_id,
            flow_id=selector.flow_id,
            step_id=selector.step_id,
            tenant_scope=selector.tenant_scope,
        )
        if previous is not None and previous.selector.deprecated_at is None:
            await self._store.mark_deprecated_by_id(
                previous.selector.selector_id, reason="superseded"
            )
        signature = sign_selector(selector, key=self._key)
        await self._store.persist(StoredSelector(selector=selector, signature_hex=signature))

    async def mark_deprecated(
        self, selector_id: UUID, *, reason: str = ""
    ) -> None:
        await self._store.mark_deprecated_by_id(selector_id, reason=reason)

    async def touch_ok(self, selector_id: UUID) -> None:
        await self._store.touch_ok_by_id(selector_id)


# ---------------------------------------------------------------------------
# Convenience: build a new selector + signature pair.
# ---------------------------------------------------------------------------


def build_signed(
    *,
    signing_key: bytes,
    site_id: str,
    flow_id: str,
    step_id: str,
    strategy: SelectorStrategy,
    value: str,
    intent_desc: str,
    tenant_scope: UUID | None = None,
    version: int = 1,
    author: SelectorAuthor = SelectorAuthor.LLM_DISCOVERY,
) -> StoredSelector:
    """Crea un Selector nuevo + su firma v2. Util para seed scripts y tests."""
    selector = Selector.new(
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
    return StoredSelector(
        selector=selector,
        signature_hex=sign_selector(selector, key=signing_key),
    )


def _now() -> datetime:
    return datetime.now(tz=UTC)
