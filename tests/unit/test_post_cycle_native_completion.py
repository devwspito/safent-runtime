"""Optional memory extraction retains native selection and safe failure reporting."""

from unittest.mock import AsyncMock

import pytest

from hermes.memory.application import post_cycle_extractor as extractor
from hermes.providers.infrastructure import native_text_completion
from hermes.runtime.model_config import ModelConfig

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openai-codex/gpt-6-astra", "custom/qwen3"])
async def test_memory_uses_same_selected_transport_without_a_private_sdk_path(monkeypatch, model):
    completion = AsyncMock(return_value="Prefiere instrucciones cortas")
    monkeypatch.setattr(native_text_completion, "complete_native_text", completion)
    config = ModelConfig(model=model)
    result = await extractor._call_extractor(
        user_message="Mensaje del usuario",
        assistant_reply="Respuesta del asistente",
        model_cfg=config,
    )
    assert result == ["Prefiere instrucciones cortas"]
    assert completion.call_args.args == (config,)
    kwargs = completion.call_args.kwargs
    assert kwargs["max_tokens"] == 256 and kwargs["timeout"] == 15.0
    assert "tools" not in kwargs and "extra_body" not in kwargs


@pytest.mark.asyncio
async def test_memory_failure_is_logged_without_provider_payload(monkeypatch, caplog):
    completion = AsyncMock(side_effect=RuntimeError("fictional-private-diagnostic"))
    monkeypatch.setattr(native_text_completion, "complete_native_text", completion)
    result = await extractor._call_extractor(
        user_message="Mensaje",
        assistant_reply="Respuesta",
        model_cfg=ModelConfig(model="custom/qwen3"),
    )
    assert result == []
    assert "hermes.memory.extractor.llm_failed" in caplog.text
    assert "fictional-private-diagnostic" not in caplog.text
