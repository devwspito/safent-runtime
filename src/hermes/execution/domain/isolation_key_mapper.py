"""IsolationKeyMapper — mapeo inyectivo superficie-física → clave de aislamiento.

Resuelve OQ-3 (data-model 006 §10) y cierra CTRL-P1-18 (threat-model §3.4):

  - La MISMA superficie física siempre produce el MISMO isolation_key.
  - Superficies DISTINTAS (kind distinto O surface_id distinto) producen keys
    que nunca colisionan.

La inyectividad la garantiza el namespace: `{kind}:{surface_id}`.
No hay hashing (no-colisiones por construcción, no por probabilidad).

Para superficies físicas únicas (teclado/ratón/display primario) se espera
que el caller pase surface_id='primary' o el seat/display real (p.ej. 'seat0',
':0'). Si hay un solo seat, el key colapsa a 'keyboard:seat0' — un dueño del
teclado real. Correcto (Constitución I: no más aislamiento del necesario).

El módulo es pure stdlib — cero dependencias de infra/HTTP/DB (Constitución DDD).
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.execution.domain.ports import InputSurfaceKey, InputSurfaceKind


@dataclass(frozen=True, slots=True)
class PhysicalSurface:
    """Identidad de una superficie física real (entrada del mapper).

    Semánticamente equivalente a InputSurfaceKey pero con nombre que expresa
    que es la superficie FÍSICA (no la clave de aislamiento lógico).
    """

    kind: InputSurfaceKind
    surface_id: str


class IsolationKeyMapper:
    """Mapper de superficie física → clave de aislamiento (isolation_key).

    Stateless: todos los métodos son @staticmethod.
    Inyectivo por construcción — la cadena '{kind}:{surface_id}' es única
    para cada (kind, surface_id), siempre que ambas coordenadas sean distintas.
    """

    @staticmethod
    def key_for(surface: PhysicalSurface) -> str:
        """Deriva el isolation_key canónico para una superficie física.

        La misma superficie → mismo key (determinismo).
        Superficies distintas → keys distintos (inyectividad).
        """
        return f"{surface.kind}:{surface.surface_id}"

    @staticmethod
    def surface_key_for(surface: PhysicalSurface) -> InputSurfaceKey:
        """Construye el InputSurfaceKey correspondiente a una PhysicalSurface.

        Conveniencia para el caller que necesita el VO de dominio.
        """
        return InputSurfaceKey(kind=surface.kind, surface_id=surface.surface_id)
