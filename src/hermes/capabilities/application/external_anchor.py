"""T042 — ExternalAnchorPort (CTRL-8/AUD-2/TOP-4).

Puerto para anclar el head_hash del audit chain a un almacenamiento externo
append-only. El anclaje externo es la única forma de detectar que la cadena
local fue reescrita por root local (threat-model AUD-2).

Controles:
  - CTRL-8: anclaje externo append-only del head_hash.
  - AUD-2: truncado de cadena detectado (head local ≠ ancla).
  - TOP-4: no-repudio — sin ancla externa, SC-006 es falso contra root local.

Implementaciones:
  - WormFileAnchor: append-only a un fichero local (misma máquina, diferente
    ruta de acceso). Simple y funcional en P0; mitiga tampering accidental.
    No protege contra root que tenga acceso al fichero ancla.
  - TsaExternalAnchor: esqueleto RFC-3161 (TSA); requiere servicio externo.
    Documentado como path a seguir para no-repudio fuerte.

Capa: application/ExternalAnchorPort (Protocol), infrastructure/ para impls.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExternalAnchorPort(Protocol):
    """Puerto para anclar el head_hash del audit chain externamente.

    El anclaje es append-only: cada llamada a anchor() agrega el hash sin
    sobreescribir entradas previas. verify() compara el head local contra
    la última ancla registrada.

    Fail-closed: verify() retorna False si no hay ancla registrada o si
    el hash local no coincide con la última ancla.
    """

    async def anchor(self, head_hash_hex: str) -> str:
        """Ancla el head_hash externo y devuelve una referencia opaca.

        Args:
            head_hash_hex: hex del signed_payload_hash_hex de la última
                AuditEntry firmada.

        Returns:
            Referencia opaca del anclaje (fichero offset, TSA token, etc.).
        """
        ...

    async def get_latest(self) -> str | None:
        """Devuelve el último head_hash anclado; None si no hay anclas."""
        ...

    async def verify(self, local_head: str) -> bool:
        """Verifica que local_head coincide con la última ancla registrada.

        Fail-closed: False si no hay ancla o si divergen.
        Un False indica posible tampering de la cadena local (AUD-2).
        """
        ...
