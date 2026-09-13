"""Exercise the installed Hermes factory and Responses payload builder offline."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes.providers.infrastructure.native_text_completion import complete_native_text
from hermes.runtime.model_config import ModelConfig

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-terra"])
async def test_actual_codex_factory_adapter_excludes_chat_only_wire_fields(monkeypatch, model):
    auxiliary = pytest.importorskip("agent.auxiliary_client")
    from hermes.runtime import managed_llm_bootstrap

    monkeypatch.setattr(managed_llm_bootstrap, "assert_process_admission", lambda **_: None)
    runtime_module = ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = MagicMock(
        return_value={
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "api_key": "fictional-test-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_module)
    adapter = auxiliary._CodexCompletionsAdapter(
        SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex"),
        model,
    )
    wire = {}

    def create(**kwargs):
        payload, _, _ = adapter._build_responses_kwargs(kwargs)
        wire.update(payload)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Texto de prueba",
                        tool_calls=None,
                    )
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=MagicMock(),
    )
    # Keep the real explicit factory branch but prevent all credential/network access.
    monkeypatch.setattr(auxiliary, "_build_codex_client", lambda selected: (client, selected))
    monkeypatch.setattr(
        auxiliary, "_read_codex_access_token", MagicMock(side_effect=AssertionError)
    )
    factory = MagicMock(wraps=auxiliary.resolve_provider_client)
    monkeypatch.setattr(auxiliary, "resolve_provider_client", factory)
    result = await complete_native_text(
        ModelConfig(
            model=f"openai-codex/{model}",
            extra={
                "extra_body": {
                    "chat_template_kwargs": {"enable_thinking": True},
                    "temperature": 0.9,
                },
            },
        ),
        messages=[
            {"role": "system", "content": "Texto solamente"},
            {"role": "user", "content": "Devuelve texto"},
        ],
        max_tokens=32,
        temperature=0.3,
        timeout=3.0,
    )
    assert result == "Texto de prueba"
    assert factory.call_args.kwargs["api_mode"] == "codex_responses"
    assert factory.call_args.kwargs["raw_codex"] is False
    assert wire["model"] == model and wire["store"] is False
    assert wire["instructions"] == "Texto solamente" and wire["input"]
    assert not {
        "tools",
        "messages",
        "temperature",
        "max_tokens",
        "max_output_tokens",
        "chat_template_kwargs",
        "extra_body",
    }.intersection(wire)
    client.close.assert_called_once()
