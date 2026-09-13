"""Read-only Responses probe through Hermes' native transport adapter.

The adapter shares auth headers, streaming and payload shaping with Hermes.
Never run an agent/tool loop or fall back to another provider for a probe.
"""

from typing import Any


def probe_responses_runtime(runtime: dict[str, Any], model: str) -> tuple[bool, str | None]:
    from agent.auxiliary_client import resolve_provider_client  # noqa: PLC0415

    provider = runtime.get("provider")
    if not provider or provider == "auto" or runtime.get("api_mode") != "codex_responses":
        raise ValueError("An explicit Responses runtime is required")
    client, resolved_model = resolve_provider_client(
        provider=provider,
        model=model,
        explicit_api_key=runtime.get("api_key"),
        explicit_base_url=runtime.get("base_url"),
        api_mode=runtime["api_mode"],
        raw_codex=False,
    )
    if client is None or not resolved_model:
        return False, "No se pudo preparar la conexión autorizada de este proveedor."
    try:
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
            max_tokens=32,
            timeout=20.0,
        )
        choices = getattr(response, "choices", None) or []
        content = (
            getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        )
        if not isinstance(content, str) or not content.strip():
            return False, "El modelo no devolvió una respuesta de texto."
        return True, None
    finally:
        # Hermes' adapter owns its client and exposes close(); do not close
        # shared credential pools or another task's primary client.
        close = getattr(client, "close", None)
        if callable(close):
            close()
