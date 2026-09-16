"""Native auxiliary calls pin provider/mode and never use a fallback/tool loop."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes.providers.infrastructure.native_text_completion import (
    NativeTextCompletionError,
    complete_native_text,
)
from hermes.runtime.model_config import ModelConfig

pytestmark = pytest.mark.unit


def text_response(content="Respuesta válida", *, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                )
            )
        ]
    )


@pytest.fixture
def native(monkeypatch):
    from hermes.runtime import managed_llm_bootstrap

    admission = MagicMock()
    monkeypatch.setattr(managed_llm_bootstrap, "assert_process_admission", admission)
    runtime = {
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "api_key": "fictional-test-token",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }
    runtime_resolver = MagicMock(return_value=runtime)
    runtime_module = ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = runtime_resolver
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_module)
    client = MagicMock()
    client.chat.completions.create.return_value = text_response()
    factory = MagicMock(side_effect=lambda **kwargs: (client, kwargs["model"]))
    module = ModuleType("agent.auxiliary_client")
    module.resolve_provider_client = factory
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    return SimpleNamespace(
        runtime=runtime,
        resolve=runtime_resolver,
        factory=factory,
        client=client,
        admission=admission,
    )


async def complete(config=None):
    return await complete_native_text(
        config or ModelConfig(model="openai-codex/gpt-6-astra"),
        messages=[{"role": "user", "content": "Un texto sintético"}],
        max_tokens=256,
        temperature=0.3,
        timeout=7.0,
    )


@pytest.mark.asyncio
async def test_codex_uses_explicit_native_responses_without_local_body_or_tools(native):
    assert await complete() == "Respuesta válida"
    native.factory.assert_called_once_with(
        provider="openai-codex",
        model="gpt-6-astra",
        raw_codex=False,
        explicit_api_key="fictional-test-token",
        explicit_base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
    )
    kwargs = native.client.chat.completions.create.call_args.kwargs
    assert set(kwargs) == {"model", "messages", "max_tokens", "timeout"}
    assert kwargs["timeout"] == 7.0
    native.client.close.assert_called_once()
    native.admission.assert_called_once_with(managed=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,mode,model,expected",
    [
        ("custom", "chat_completions", "qwen3", True),
        ("custom:my-local", "chat_completions", "Qwen3", True),
        ("custom", "chat_completions", "llama", False),
        ("custom", "codex_responses", "qwen3", False),
        ("openrouter", "chat_completions", "qwen/qwen3", False),
        ("anthropic", "anthropic_messages", "claude", False),
    ],
)
async def test_qwen_extension_only_belongs_to_local_custom_chat(
    native, provider, mode, model, expected
):
    native.runtime.update(provider=provider, api_mode=mode)
    await complete(ModelConfig(model=f"{provider}/{model}", native_provider=provider))
    kwargs = native.client.chat.completions.create.call_args.kwargs
    assert ("extra_body" in kwargs) is expected
    if expected:
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "tools" not in kwargs


@pytest.mark.asyncio
async def test_keyless_custom_endpoint_cannot_inherit_an_unrelated_global_key(native):
    native.runtime.update(provider="custom", api_mode="chat_completions", api_key=None)
    await complete(ModelConfig(model="custom/qwen3"))
    assert native.factory.call_args.kwargs["explicit_api_key"] == "no-key-required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_override",
    [
        {"provider": "auto"},
        {"provider": None},
        {"api_mode": "unknown"},
        {"provider": "custom", "base_url": None},
    ],
)
async def test_unusable_runtime_is_rejected_before_client_resolution(native, runtime_override):
    native.runtime.update(runtime_override)
    with pytest.raises(NativeTextCompletionError):
        await complete()
    native.factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        ModelConfig(model="auto/gpt"),
        ModelConfig(model="gpt"),
        ModelConfig(model="custom/qwen", managed=True),
    ],
)
async def test_automatic_or_managed_config_never_resolves_personal_credentials(native, config):
    with pytest.raises(NativeTextCompletionError):
        await complete(config)
    native.resolve.assert_not_called()
    native.factory.assert_not_called()


@pytest.mark.asyncio
async def test_admission_failure_never_resolves_personal_credentials(native):
    native.admission.side_effect = RuntimeError("fictional-private-diagnostic")
    with pytest.raises(NativeTextCompletionError) as error:
        await complete()
    assert "fictional-private-diagnostic" not in str(error.value)
    native.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_missing_client_never_falls_back(native):
    native.factory.side_effect = None
    native.factory.return_value = (None, None)
    with pytest.raises(NativeTextCompletionError):
        await complete()
    assert native.factory.call_count == 1
    native.client.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
async def test_request_error_is_redacted_and_client_closed_without_retry(native):
    native.client.chat.completions.create.side_effect = RuntimeError("fictional-private-diagnostic")
    with pytest.raises(NativeTextCompletionError) as error:
        await complete()
    assert "fictional-private-diagnostic" not in str(error.value)
    assert error.value.__suppress_context__ is True
    assert native.factory.call_count == native.client.chat.completions.create.call_count == 1
    native.client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[]),
        text_response(""),
        text_response(None),
        text_response("some text", tool_calls=[{"name": "unexpected"}]),
    ],
)
async def test_no_empty_or_tool_response_is_treated_as_success(native, response):
    native.client.chat.completions.create.return_value = response
    with pytest.raises(NativeTextCompletionError):
        await complete()
    native.client.close.assert_called_once()
