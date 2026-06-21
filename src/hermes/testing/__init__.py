"""Helpers para tests del consumidor de Hermes.

Permite a cada vertical testear su composicion sin invocar un LLM real:

    from hermes.testing import FakeReasoningEngine, scripted_response

    engine = FakeReasoningEngine(
        scripted=[
            scripted_response(
                narrative="Pago fraccionado 202 listo, pendiente tu OK.",
                proposals=[...],
            ),
        ],
    )

    output = await engine.run_cycle(decision_context)
"""

from hermes.testing.fakes import FakeReasoningEngine, scripted_response

__all__ = ["FakeReasoningEngine", "scripted_response"]
