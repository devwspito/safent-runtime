"""nous_ping — valida un provider EJECUTANDO el runtime real (Nous AIAgent).

El test de onboarding ("Probar") NO usa litellm: hace una completion mínima de
un token a través de la MISMA `run_agent.AIAgent` que ejecuta el daemon Nous.
Así "Probar pasa" ⟺ "el chat funcionará" — mismo cliente, mismos adapters por
provider (OpenAI/Anthropic/Kimi/…), mismo manejo de params/model-string/auth.
Cero dialecto paralelo: litellm queda fuera del flujo de validación.

Por qué aquí (shell-server) y no por D-Bus al daemon: la validación que importa
es la de la LLAMADA AL MODELO (key/modelo/params), que es código de Nous y es
idéntica en ambos procesos. La infra daemon (cola/broker/stream) ya está probada
y no es lo que el test necesita ejercitar. Usamos EXACTAMENTE el mismo
`litellm_model_string(provider, model)` + `base_url` que `provider_config_source`
para que el ModelConfig coincida con el del runtime.
"""

from __future__ import annotations

import asyncio
import logging

from hermes.shell_server.providers.domain import Provider, litellm_model_string

logger = logging.getLogger("hermes.shell_server.chat.nous_ping")

_PING_SYSTEM = "Eres un validador de conectividad. Responde únicamente con: OK"
_PING_USER = "OK"


def _run_nous_ping(model: str, api_key: str | None, base_url: str | None) -> str:
    """Síncrono: construye una AIAgent mínima (sin tools) y hace UNA completion.

    Devuelve el `final_response` (str). Lanza en cualquier fallo real del runtime
    (auth/modelo/params/red) — el caller lo traduce a (False, mensaje). El import
    de `run_agent` es perezoso (paquete pesado; solo al validar).
    """
    from run_agent import AIAgent  # noqa: PLC0415

    agent = AIAgent(
        model=model,
        api_key=api_key,
        base_url=base_url or None,
        max_iterations=2,
        enabled_toolsets=[],
        save_trajectories=False,
        quiet_mode=True,
        ephemeral_system_prompt=_PING_SYSTEM,
    )
    result = agent.run_conversation(_PING_USER)
    if isinstance(result, dict):
        return str(result.get("final_response") or "")
    return str(result or "")


async def ping(
    *, provider: Provider, api_key: str | None
) -> tuple[bool, str | None]:
    """Valida el provider a través del runtime Nous real. Devuelve (ok, error).

    El error es el del runtime/provider tal cual, para que coincida con lo que
    vería el chat. NO se loguea ni el texto ni la key (CTRL-P1-9 / PII).
    """
    model = litellm_model_string(provider, provider.default_model)
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, _run_nous_ping, model, api_key, provider.base_url
        )
    except Exception as exc:  # noqa: BLE001  (cualquier fallo del runtime real)
        logger.info(
            "hermes.shell_server.nous_ping.failed",
            extra={"provider_kind": str(provider.kind), "error_type": type(exc).__name__},
        )
        return False, _friendly(exc)

    if resp.strip():
        return True, None
    return False, "El modelo no devolvió respuesta."


def _friendly(exc: Exception) -> str:
    """Mensaje accionable a partir de la excepción del runtime (truncado, 1 línea)."""
    msg = str(exc).strip().replace("\n", " ")
    out = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return out[:400]
