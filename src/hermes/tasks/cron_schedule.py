"""Vocabulario único de cron para el subsistema de tareas de Nous.

Un solo sitio que envuelve croniter (MIT) para TODO el stack de scheduling:
  - `next_fire(expr, after)`  — próximo disparo > after   (UI/control plane: next_run_at)
  - `prev_fire(expr, before)` — disparo más reciente ≤ before (timer source: ¿qué slot toca?)

Antes había DOS sitios llamando croniter (control_plane._cron_next_fire y un
get_prev inline en el timer source). Esto es la consolidación: el firing loop y
el cálculo de UI hablan el MISMO idioma de cron, fail-soft idéntico.

Clock-injectable: el caller pasa el instante explícito; nunca llama datetime.now().
Fail-soft: expresión inválida o croniter ausente → None (el caller decide; el
tablero nunca se cae y el timer nunca inventa un horario).
"""
from __future__ import annotations

from datetime import datetime


def next_fire(cron_expr: str, *, after: datetime) -> datetime | None:
    """Próximo disparo cron estrictamente posterior a `after`, o None.

    Soporta la gramática crontab completa (rangos, pasos, listas, meses/días
    nombrados) vía croniter. None para expresiones no reconocibles.
    """
    try:
        from croniter import croniter  # noqa: PLC0415

        return croniter(cron_expr, after).get_next(datetime)
    except (ValueError, KeyError, TypeError, ImportError):
        return None


def prev_fire(cron_expr: str, *, before: datetime) -> datetime | None:
    """Disparo cron más reciente igual o anterior a `before`, o None.

    Lo usa el timer source para decidir el slot vencido a disparar: devolver el
    más reciente (no iterar get_next) evita backfillear ráfagas de slots perdidos
    tras un downtime — dispara como mucho el último.
    """
    try:
        from croniter import croniter  # noqa: PLC0415

        return croniter(cron_expr, before).get_prev(datetime)
    except (ValueError, KeyError, TypeError, ImportError):
        return None
