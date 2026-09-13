"""One tool-free text call through the selected native provider's transport.

Do not use Hermes' automatic auxiliary/recovery ladder here: a selected provider
must never silently fall back to a different account, provider, or model.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from hermes.runtime.model_config import ModelConfig


class NativeTextCompletionError(RuntimeError):
    """Safe caller-visible error; never contains a credential or provider body."""


async def complete_native_text(
    config: ModelConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float,  # noqa: ASYNC109 — enforced by the native SDK off-loop
    temperature: float | None = None,
) -> str:
    """Use the synchronous native adapter off-loop, with its request timeout."""
    return await asyncio.to_thread(
        _complete,
        config,
        messages,
        max_tokens,
        timeout,
        temperature,
    )


def _complete(  # noqa: PLR0912, PLR0915 — one owned-client lifecycle, closed on every branch
    config: ModelConfig,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float,
    temperature: float | None,
) -> str:
    client = None
    try:
        # These optional local calls have no isolated Enterprise grant contract.
        # Reject before importing or resolving a personal/native credential.
        if config.managed:
            raise NativeTextCompletionError(
                "La llamada auxiliar no está disponible en modo Enterprise."
            )
        from hermes.runtime.managed_llm_bootstrap import assert_process_admission  # noqa: PLC0415

        assert_process_admission(managed=False)
        from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: PLC0415

        from hermes.providers.infrastructure.nous_provider_adapter import (  # noqa: PLC0415
            nous_request_from_model_config,
        )

        request, model = nous_request_from_model_config(config)
        requested = config.native_provider or request.requested
        if not requested or requested == "auto" or not model:
            raise NativeTextCompletionError("Selecciona un proveedor y un modelo explícitos.")
        runtime = resolve_runtime_provider(
            requested=requested,
            explicit_api_key=request.explicit_api_key,
            explicit_base_url=request.explicit_base_url,
            target_model=model,
        )
        provider = runtime.get("provider")
        mode = runtime.get("api_mode")
        if (
            not provider
            or provider == "auto"
            or mode
            not in {
                "chat_completions",
                "codex_responses",
                "anthropic_messages",
            }
        ):
            raise NativeTextCompletionError("El transporte del proveedor no está disponible.")
        is_custom = str(provider).split(":", 1)[0] == "custom"
        key = runtime.get("api_key")
        if is_custom:
            if not runtime.get("base_url"):
                raise NativeTextCompletionError(
                    "El proveedor local no tiene un endpoint configurado."
                )
            # Preserve the resolved snapshot, not a second named-custom lookup.
            # A keyless local endpoint must not inherit OPENAI_API_KEY globally.
            provider = "custom"
            key = key or "no-key-required"
        from agent.auxiliary_client import resolve_provider_client  # noqa: PLC0415

        client, resolved_model = resolve_provider_client(
            provider=provider,
            model=model,
            explicit_api_key=key,
            explicit_base_url=runtime.get("base_url"),
            api_mode=mode,
            raw_codex=False,
        )
        if client is None or not resolved_model:
            raise NativeTextCompletionError("No se pudo preparar el proveedor seleccionado.")
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        # The native Responses adapter translates the chat-shaped call and
        # deliberately omits unsupported max_tokens/temperature on Codex wire.
        if mode != "codex_responses" and temperature is not None:
            kwargs["temperature"] = temperature
        if is_custom and mode == "chat_completions" and "qwen" in model.lower():
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        response = client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        content = getattr(message, "content", None)
        if (
            not isinstance(content, str)
            or not content.strip()
            or getattr(message, "tool_calls", None)
        ):
            raise NativeTextCompletionError("El modelo no devolvió una respuesta de texto válida.")
        return content.strip()
    except NativeTextCompletionError:
        raise
    except Exception:
        # The transport exception may include request bodies, URLs or tokens.
        raise NativeTextCompletionError(
            "No se pudo completar la llamada al proveedor seleccionado."
        ) from None
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            # Never replace the safe result/error with transport details.
            with suppress(Exception):
                close()
