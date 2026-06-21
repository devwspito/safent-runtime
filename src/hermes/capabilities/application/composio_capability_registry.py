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
  - NUNCA devuelve binding — el broker hace fail-closed (REJECTED_BY_POLICY).
  - WRITE tools de Composio van al HITL flow desde la CapturingToolHost (handler=None).
  - Si por alguna razón llegaran aquí como proposal, fail-closed es correcto.

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
        (contiene '_' con al menos 2 segmentos) → binding dinámico.
        Solo devuelve binding para READ slugs de Composio. WRITE slugs
        devuelven None (fail-closed en el broker).
        """
        static = self._static.resolve(tool_name)
        if static is not None:
            return static

        if not _looks_like_composio_slug(tool_name):
            return None

        if not _is_composio_read_slug(tool_name):
            # WRITE Composio proposal — el broker hace fail-closed.
            # Esto no debería ocurrir: WRITE specs tienen handler=None y van
            # al path WRITE_PROPOSAL sin pasar por el broker como READ.
            return None

        return CapabilityBinding(
            tool_name=tool_name,
            surface_kind=SurfaceKind.API_CALL,
            required_capability=None,  # READ de Composio: no requiere consent per-capability
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="composio",
        )


def _looks_like_composio_slug(name: str) -> bool:
    """True si el nombre tiene la forma TOOLKIT_VERB_NOUN (≥2 segmentos con '_')."""
    return "_" in name and len(name.split("_")) >= 2
