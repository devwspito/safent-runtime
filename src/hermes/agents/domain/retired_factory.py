"""Historical identities only: no packaged personas, catalog or seed logic.

Keep these IDs reserved so old tasks cannot silently become the default agent.
Existing explicitly cloud-managed profiles are not factory data. No rows, task
IDs, conversations or audit history are deleted by this retirement.
"""

RETIRED_FACTORY_IDS = frozenset(
    {
        "roster-atencion-redactor",
        "roster-atencion-soporte",
        "roster-atencion-traductor",
        "roster-codigo-arquitecto",
        "roster-codigo-desarrollador",
        "roster-codigo-revisor",
        "roster-creatividad-director",
        "roster-creatividad-guionista",
        "roster-creatividad-naming",
        "roster-finanzas-analista",
        "roster-finanzas-contable",
        "roster-finanzas-fiscal",
        "roster-legal-contratos",
        "roster-legal-cumplimiento",
        "roster-legal-revisor",
        "roster-marketing-copywriter",
        "roster-marketing-seo",
        "roster-marketing-social",
        "roster-ops-automatizador",
        "roster-ops-ejecutivo",
        "roster-ops-proyectos",
        "roster-research-datos",
        "roster-research-informes",
        "roster-research-investigador",
        "roster-ventas-cierre",
        "roster-ventas-outreach",
        "roster-ventas-prospector",
    }
)


class FactoryAgentRetired(ValueError):
    """The owner removed packaged agents; this task needs a new explicit target."""


def require_not_retired(agent_id: str | None, *, managed_by: str | None = None) -> None:
    if agent_id in RETIRED_FACTORY_IDS and managed_by != "cloud":
        raise FactoryAgentRetired(
            "Este agente empaquetado fue retirado. Elige un destino actual para una nueva tarea."
        )
