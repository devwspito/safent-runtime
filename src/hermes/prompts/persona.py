"""PersonaSpec: identidad profesional + reglas de oro + plantillas de comunicacion.

Cada vertical instancia su propio PersonaSpec. Hermes inyecta los campos al
system prompt construido por `DefaultPromptBuilder`.

Ejemplo gestoria-agent (resumen):
    PersonaSpec(
        name="Hermes",
        role="Oficial multidisciplinar de despacho con 4 anios de experiencia",
        language="es-ES",
        register="castellano de despacho, tutea al titular, usted al cliente",
        primary_mission="back-office completo: fiscal+laboral+contable+mercantil",
        golden_rules=(
            "Sujetos a SII NO presentan 347.",
            "Retenciones e ingresos a cuenta nunca son aplazables.",
            ...
        ),
        forbidden_phrases=(
            "como asistente", "como modelo de lenguaje", "voy a procesar",
            "espero que esto te ayude", "no dudes en contactarme",
        ),
        out_of_scope=(
            "Penal-tributario art. 305 CP",
            "M&A complejos (escisiones/fusiones grandes)",
            "Recursos contencioso-administrativos",
            ...
        ),
    )

Ejemplo oposads-agent:
    PersonaSpec(
        name="Hermes",
        role="Optimizador de campanas de ads para academias",
        language="es-ES",
        register="profesional, directo, KPIs siempre con numero",
        primary_mission="proponer cambios en campanas Meta/Google ante anomalias",
        golden_rules=(
            "CPA por encima de umbral 2x => pausar ad set y proponer revision.",
            "Cambio de budget > 10% requiere HITL siempre.",
            ...
        ),
        forbidden_phrases=(...),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PersonaSpec:
    """Identidad profesional inyectable.

    Atributos:
        name:               nombre operativo ("Hermes").
        role:               cargo equivalente humano ("oficial junior-senior...").
        language:           tag BCP-47 ("es-ES", "en-US"). El system prompt se renderiza
                            en este idioma.
        register:           tono y forma ("castellano de despacho, tutea al titular...").
        primary_mission:    una frase de "que es lo que hace".
        golden_rules:       lista de invariantes profesionales (memoria operativa).
        forbidden_phrases:  frases que NUNCA debe emitir (anti jerga IA).
        out_of_scope:       lo que NO hace; debe escalar.
        escalation_triggers: situaciones que disparan escalado humano.
        signature_template: como firma sus comunicaciones ("Hermes — back-office de {despacho}").
    """

    name: str
    role: str
    language: str
    register: str
    primary_mission: str
    golden_rules: tuple[str, ...] = field(default_factory=tuple)
    forbidden_phrases: tuple[str, ...] = field(default_factory=tuple)
    out_of_scope: tuple[str, ...] = field(default_factory=tuple)
    escalation_triggers: tuple[str, ...] = field(default_factory=tuple)
    signature_template: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("PersonaSpec.name is required")
        if not self.role:
            raise ValueError("PersonaSpec.role is required")
        if not self.language:
            raise ValueError("PersonaSpec.language is required")
        if not self.primary_mission:
            raise ValueError("PersonaSpec.primary_mission is required")
