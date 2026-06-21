"""FakeExternalAnchor — fake de ExternalAnchorPort para tests unitarios.

Implementa ExternalAnchorPort (T042/US2). Registra los hashes anclados
en memoria para assertions en tests.
"""

from __future__ import annotations


class FakeExternalAnchor:
    """Fake de ExternalAnchorPort (US2/T042).

    Registra en memoria todos los hashes anclados. Útil para verificar que
    SqliteAuditRepository llama a anchor() tras cada append.
    """

    def __init__(self) -> None:
        self.anchored: list[str] = []

    async def anchor(self, head_hash_hex: str) -> str:
        """Registra el head_hash como si lo anclara externamente.

        Returns:
            Referencia opaca (el propio hash — suficiente en tests).
        """
        self.anchored.append(head_hash_hex)
        return head_hash_hex

    async def get_latest(self) -> str | None:
        """Devuelve el último hash anclado; None si no hay anclas."""
        return self.anchored[-1] if self.anchored else None

    async def verify(self, local_head: str) -> bool:
        """Verifica que local_head coincide con la última ancla.

        Fail-closed: False si no hay anclas o si divergen.
        """
        if not self.anchored:
            return False
        return local_head == self.anchored[-1]
