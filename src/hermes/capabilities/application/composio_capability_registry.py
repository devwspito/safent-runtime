"""ComposioCapabilityRegistry — resolución dinámica de slugs Composio.

Los slugs Composio (GMAIL_GET_EMAIL, GOOGLEDRIVE_LIST_FILES, …) son dinámicos
(se descubren en runtime de los apps conectados) y no pueden vivir en la tabla
estática de CapabilityRegistry. Este adapter decora la registry estática:

  1. Intenta resolver en la registry estática (prioridad).
  2. Si no está registrado Y el nombre parece un slug Composio (contiene '_'):
     classifica el verbo y devuelve un binding dinámico con executor="composio"
     y required_capability=None.

Binding para READ de Composio:
  - risk=LOW, auto_executable=True, executor="composio".
  - El broker despacha via ComposioSurfaceAdapter (SurfaceKind.API_CALL).
  - Taint garantizado por el adapter dispatcher (tag "composio" en ToolSpec).

Binding para WRITE de Composio:
  - risk=HIGH, auto_executable=False, executor="composio".
  - El broker EXIGE HITL (tarjeta de aprobación + TOTP) y solo ejecuta vía
    ComposioSurfaceAdapter tras la aprobación del dueño. NUNCA auto-ejecuta.
  - Antes devolvía None → "no registrado" → el agente no podía enviar/crear/borrar
    en NINGUNA integración ni con aprobación (el HITL flow por CapturingToolHost solo
    aplica al engine litellm; el engine nous despacha WRITE externas por el broker).
    HIGH+no-auto mantiene el modelo soberano: nada se ejecuta hasta que el dueño
    aprueba; un WRITE derivado de contenido no confiable ya se eleva a HIGH por taint
    en _compute_effective_risk.

Nota: el nombre del tool en el CapturingToolHost es el slug lowercased
(e.g. "gmail_get_email"). La classificación necesita el slug uppercased.
Este adapter canonicaliza antes de clasificar.

IMPORTANTE: este módulo replica la lógica de _READ_VERBS/classify_tool_risk
inline para evitar importar hermes.runtime.composio_tool_specs (que a su vez
importa hermes.integrations.composio.composio_client, cuya dependencia del
SDK composio no está disponible en todos los entornos de test). El set de
verbos READ debe mantenerse sincronizado con composio_tool_specs._READ_VERBS.

Capa: application (combina domain ports sin I/O directa).
"""

from __future__ import annotations

from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    CapabilityRegistryPort,
    RiskLevel,
)

# Verbos READ — copia explícita de composio_tool_specs._READ_VERBS.
# Mantenidas sincronizadas; EXPORT/DOWNLOAD excluidos intencionalmente (Fix-4).
_READ_VERBS: frozenset[str] = frozenset(
    {
        "GET",
        "LIST",
        "FETCH",
        "SEARCH",
        "FIND",
        "READ",
        "SHOW",
        "VIEW",
        "QUERY",
        "DESCRIBE",
        "RETRIEVE",
        "CHECK",
        "PREVIEW",
        "INSPECT",
        "STATUS",
        "PING",
    }
)


def _is_composio_read_slug(slug_lower: str) -> bool:
    """True si el slug lowercased corresponde a un verbo READ de Composio.

    Formato Composio: TOOLKIT_VERB_NOUN → lowercased: toolkit_verb_noun.
    parts[1] es el verbo. Conservador: sin verbo reconocido → False.
    """
    parts = slug_lower.upper().split("_")
    if len(parts) < 2:
        return False
    return parts[1] in _READ_VERBS


class ComposioCapabilityRegistry:
    """CapabilityRegistryPort que resuelve slugs Composio dinámicamente.

    Args:
        static_registry: registry base (CapabilityRegistry o cualquier impl).
    """

    def __init__(self, *, static_registry: CapabilityRegistryPort) -> None:
        self._static = static_registry

    def resolve(self, tool_name: str) -> CapabilityBinding | None:
        """Resuelve tool_name a binding.

        Prioriza la registry estática. Si no está y parece slug Composio
        (contiene '_' con al menos 2 segmentos) → binding dinámico:
          - READ slug  → LOW + auto_executable (fluye tras taint + kill-switch).
          - WRITE slug (incl. export/download) → HIGH + auto_executable=False:
            el broker EXIGE HITL (tarjeta + TOTP) antes de despachar vía
            ComposioSurfaceAdapter. NUNCA auto-ejecuta. (El diseño anterior
            devolvía None, que bloqueaba TODA escritura incluso con aprobación
            del dueño — corregido para habilitar writes gobernados por HITL.)
        """
        static = self._static.resolve(tool_name)
        if static is not None:
            return static

        if not _looks_like_composio_slug(tool_name):
            return None

        if _is_composio_read_slug(tool_name):
            # READ: auto-ejecutable, LOW (el broker aplica el gate READ + taint).
            return CapabilityBinding(
                tool_name=tool_name,
                surface_kind=SurfaceKind.API_CALL,
                required_capability=None,  # READ de Composio: sin consent per-capability
                risk=RiskLevel.LOW,
                auto_executable=True,
                executor="composio",
            )

        # WRITE: HIGH + NO auto-ejecutable → el broker EXIGE HITL (tarjeta + TOTP) y
        # solo ejecuta vía ComposioSurfaceAdapter tras la aprobación del dueño.
        return CapabilityBinding(
            tool_name=tool_name,
            surface_kind=SurfaceKind.API_CALL,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="composio",
        )


def _looks_like_composio_slug(name: str) -> bool:
    """True si el nombre tiene la forma TOOLKIT_VERB_NOUN (≥2 segmentos con '_')."""
    return "_" in name and len(name.split("_")) >= 2
