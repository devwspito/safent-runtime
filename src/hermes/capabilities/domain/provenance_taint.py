"""T035 — ProvenanceTaint VO + lógica de taint de procedencia (CTRL-5/TOP-1).

threat-model §3/TOP-1 (confused-deputy / prompt-injection):
  El agente puede leer contenido del mundo (ficheros, web, emails) que
  inyecta instrucciones. Si la propuesta deriva de ese contenido untrusted,
  se eleva a HITL forzado, ignorando cualquier consent amplio.

Funciones puras: sin efectos laterales, sin framework.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.capabilities.domain.ports import CapabilityBinding, RiskLevel

# Rutas de fichero cuya LECTURA exige HITL cuando el ciclo está tainteado.
# Justificación: bajo taint el loop puede re-exportar lo que lee. La postura
# conservadora es exigir HITL para cualquier ruta que pueda contener
# credenciales, tokens, o datos de identidad.
# NO es la lista de rutas bloqueadas siempre — solo bajo taint del ciclo.
_SENSITIVE_PATH_PREFIXES: frozenset[str] = frozenset(
    {
        "/home/",
        "/root/",
        "/var/lib/hermes/",
        "/.ssh/",
        "/.aws/",
        "/.config/",
        "/etc/hermes/credentials",
        "/etc/hermes/secrets",
        "/etc/hermes/api_keys",
    }
)


@dataclass(frozen=True, slots=True)
class ProvenanceTaint:
    """Procedencia de una ToolCallProposal en este ciclo.

    Attributes:
        derived_from_untrusted_content:
            True si el agente leyó un fichero, página web, email o cualquier
            contenido externo en el ciclo actual y la propuesta deriva de ese
            contexto. Poblado por el AgentLoopOrchestrator desde el
            ReasoningEngine output (flag `read_external_content`).
    """

    derived_from_untrusted_content: bool = False


def requires_forced_hitl(
    taint: ProvenanceTaint,
    binding: CapabilityBinding,
) -> bool:
    """True si el broker DEBE elevar esta propuesta a HITL sin importar consent.

    Regla CTRL-5 / TOP-1:
      - Si la propuesta deriva de contenido untrusted (taint) Y la capability
        es HIGH → HITL forzado (el consent amplio se ignora).
      - Si la propuesta deriva de contenido untrusted Y la capability es LOW
        pero auto_executable=False → también HITL forzado (p.ej. un write
        no auto-ejecutable que proviene de una web leída).
      - Si la propuesta deriva de contenido untrusted Y la capability es LOW
        Y auto_executable=True (solo read_file/list_dir) → se permite
        continuar sin HITL (leer desde contenido untrusted es safe; el
        peligro es actuar a partir de él).
      - Sin taint → la regla no aplica; el broker usa su lógica normal de
        riesgo + consent.

    NOTA: read_file de rutas sensibles bajo taint se eleva en
    requires_sensitive_path_hitl(), invocada por separado en el broker.

    Pure function: sin efectos laterales.
    """
    if not taint.derived_from_untrusted_content:
        return False

    # Acción HIGH derivada de contenido untrusted => siempre HITL.
    if binding.risk is RiskLevel.HIGH:
        return True

    # LOW no auto-ejecutable desde untrusted => HITL.
    # LOW auto-ejecutable (read_file/list_dir) desde untrusted: permitido —
    # leer no es el vector de daño en sí mismo.
    return not binding.auto_executable


def is_sensitive_path_read_under_taint(
    taint: ProvenanceTaint,
    tool_name: str,
    parameters: dict,
) -> bool:
    """True si read_file de ruta sensible bajo taint exige HITL.

    Fix-3 (CTRL-5 / TOP-1 extensión):
    Bajo taint (derived_from_untrusted_content=True), leer rutas que contienen
    credenciales, tokens o datos de identidad YA ES un vector de daño: el loop
    puede re-exportar lo que lee. Estas lecturas deben pasar por HITL.

    La condición tiene tres partes (AND):
      1. El ciclo está tainteado.
      2. La tool es read_file (list_dir no expone contenido binario/secreto).
      3. El path pertenece al conjunto de rutas sensibles.

    Pure function: sin I/O.
    """
    if not taint.derived_from_untrusted_content:
        return False
    if tool_name != "read_file":
        return False
    path = str(parameters.get("path", ""))
    return _path_is_sensitive(path)


def _path_is_sensitive(path: str) -> bool:
    """True si el path pertenece al allow-list de rutas sensibles."""
    if not path:
        return False
    return any(path.startswith(prefix) for prefix in _SENSITIVE_PATH_PREFIXES)
