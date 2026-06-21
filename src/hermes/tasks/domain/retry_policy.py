"""RetryPolicy value object — backoff exponencial puro (FR-006).

Calcula el delay de reintento: base_seconds * 2^attempts, con tope.
Domain layer: puro, sin I/O, sin framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Política de reintentos con backoff exponencial.

    Args:
        max_attempts: número máximo de reintentos (default 3).
        base_seconds: base del backoff en segundos (default 30).
        cap_seconds: tope máximo del delay en segundos (default 3600 = 1 h).
    """

    max_attempts: int = 3
    base_seconds: int = 30
    cap_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser >= 1")
        if self.base_seconds < 1:
            raise ValueError("base_seconds debe ser >= 1")
        if self.cap_seconds < self.base_seconds:
            raise ValueError("cap_seconds debe ser >= base_seconds")

    def delay_seconds(self, attempts: int) -> int:
        """Calcula el delay para el attempt-ésimo reintento.

        Formula: min(base_seconds * 2^attempts, cap_seconds).
        attempts=0 => base_seconds * 1 = base_seconds.
        """
        raw = self.base_seconds * (2 ** attempts)
        return min(raw, self.cap_seconds)

    def next_available_at(self, attempts: int) -> datetime:
        """Retorna el datetime UTC a partir del cual el item es re-elegible."""
        return datetime.now(tz=UTC) + timedelta(seconds=self.delay_seconds(attempts))

    def is_exhausted(self, attempts: int) -> bool:
        """True si el número de intentos realizados agota la política."""
        return attempts >= self.max_attempts
