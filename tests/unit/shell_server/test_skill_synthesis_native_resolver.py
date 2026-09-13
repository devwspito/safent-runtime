"""Skill synthesis uses the selected native transport, including OAuth Responses."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hermes.providers.infrastructure.native_text_completion import NativeTextCompletionError
from hermes.runtime.model_config import ModelConfig
from hermes.shell_server.skills.skill_synthesis import NoActiveProvider, synthesize_skill_md

pytestmark = pytest.mark.unit
_DB_PATH = Path("/fake/shell-state.db")
_RESOLVE = "hermes.runtime.provider_config_source.resolve_model_config"
_COMPLETE = "hermes.providers.infrastructure.native_text_completion.complete_native_text"
_DOCUMENT = "===SKILL_START===\n---\ndescription: Hace algo\n---\n# Mi skill\n===SKILL_END==="


@pytest.mark.asyncio
async def test_missing_provider_never_calls_a_transport():
    with (
        patch(_RESOLVE, return_value=None),
        patch(_COMPLETE, new_callable=AsyncMock) as complete,
        pytest.raises(NoActiveProvider),
    ):
        await synthesize_skill_md(name="Mi skill", description="Hace algo", db_path=_DB_PATH)
    complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,model,base_url",
    [
        ("openai-codex", "gpt-6-astra", None),
        ("custom", "qwen3", "https://local-model.example/v1"),
        ("anthropic", "claude-sonnet", None),
    ],
)
async def test_native_selection_including_oauth_without_base_url(provider, model, base_url):
    config = ModelConfig(model=f"{provider}/{model}", native_provider=provider, base_url=base_url)
    with (
        patch(_RESOLVE, return_value=config) as resolve,
        patch(_COMPLETE, new_callable=AsyncMock, return_value=_DOCUMENT) as complete,
    ):
        result = await synthesize_skill_md(
            name="Mi skill",
            description="Hace algo",
            db_path=_DB_PATH,
            timeout=12.0,
        )
    resolve.assert_called_once_with(_DB_PATH)
    assert complete.call_args.args == (config,)
    kwargs = complete.call_args.kwargs
    assert kwargs["timeout"] == 12.0 and kwargs["max_tokens"] == 2000
    assert [message["role"] for message in kwargs["messages"]] == ["system", "user"]
    assert "Hace algo" in kwargs["messages"][1]["content"]
    assert "name: mi-skill" in result and "version: 1" in result
    assert "===SKILL_" not in result


@pytest.mark.asyncio
async def test_native_failure_is_not_an_empty_fabricated_document():
    config = ModelConfig(model="openai-codex/gpt-6-astra")
    with (
        patch(_RESOLVE, return_value=config),
        patch(
            _COMPLETE,
            new_callable=AsyncMock,
            side_effect=NativeTextCompletionError("No disponible"),
        ),
        pytest.raises(NativeTextCompletionError, match="No disponible"),
    ):
        await synthesize_skill_md(name="Mi skill", description="Hace algo", db_path=_DB_PATH)
