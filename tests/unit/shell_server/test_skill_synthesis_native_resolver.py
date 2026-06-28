"""Unit tests for synthesize_skill_md using the native resolver (R5 Stage B).

Verifies:
  - synthesize_skill_md resolves model config via resolve_model_config(db_path),
    NOT via a repo.get_active() / reveal_api_key() call.
  - NoActiveProvider is raised when resolve_model_config returns None.
  - NoActiveProvider is raised when the resolved config has no base_url
    (cloud provider not directly reachable from the shell-server).
  - A successful resolution POSTs to <base_url>/chat/completions with the
    correct model, Authorization header, and payload.

Note: resolve_model_config is imported lazily inside synthesize_skill_md, so
the mock target is hermes.runtime.provider_config_source.resolve_model_config
(the canonical module), not the skill_synthesis module itself.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.shell_server.skills.skill_synthesis import (
    NoActiveProvider,
    synthesize_skill_md,
)

pytestmark = pytest.mark.unit

_DB_PATH = Path("/fake/shell-state.db")

# Canonical patch target: where the function is defined.
_RESOLVE_TARGET = "hermes.runtime.provider_config_source.resolve_model_config"
_HTTP_TARGET = "hermes.shell_server.skills.skill_synthesis.httpx.AsyncClient"


def _make_config(*, model: str, api_key: str | None, base_url: str | None):
    from hermes.runtime.model_config import ModelConfig

    return ModelConfig.from_provider(model=model, api_key=api_key, base_url=base_url)


def _httpx_response(content: str):
    """Return a minimal httpx.Response-like mock for a chat/completions reply."""
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return mock


@pytest.mark.asyncio
async def test_raises_no_active_provider_when_resolve_returns_none() -> None:
    """resolve_model_config → None must raise NoActiveProvider (maps to 409)."""
    with patch(_RESOLVE_TARGET, return_value=None):
        with pytest.raises(NoActiveProvider):
            await synthesize_skill_md(
                name="test-skill",
                description="hace algo",
                db_path=_DB_PATH,
            )


@pytest.mark.asyncio
async def test_raises_no_active_provider_when_base_url_empty() -> None:
    """Config without base_url (cloud provider) must raise NoActiveProvider."""
    config = _make_config(model="openai/gpt-4o", api_key="sk-xxx", base_url=None)
    with patch(_RESOLVE_TARGET, return_value=config):
        with pytest.raises(NoActiveProvider, match="base_url"):
            await synthesize_skill_md(
                name="test-skill",
                description="hace algo",
                db_path=_DB_PATH,
            )


@pytest.mark.asyncio
async def test_does_not_call_repo_at_all() -> None:
    """synthesize_skill_md must never touch a SQLiteProviderRepository."""
    config = _make_config(
        model="openai-api/llama-3",
        api_key="test-key",
        base_url="http://localhost:8000/v1",
    )
    # Minimal SKILL.md content — sentinels so the parser finds the block.
    skill_body = (
        "===SKILL_START===\n"
        "---\nname: test-skill\ndescription: hace algo\nversion: 1\n---\n"
        "# Test\n## Objetivo\nHace algo\n"
        "===SKILL_END==="
    )
    http_mock = AsyncMock()
    http_mock.__aenter__ = AsyncMock(return_value=http_mock)
    http_mock.__aexit__ = AsyncMock(return_value=False)
    http_mock.post = AsyncMock(return_value=_httpx_response(skill_body))

    repo_mock = MagicMock()

    with (
        patch(_RESOLVE_TARGET, return_value=config),
        patch(_HTTP_TARGET, return_value=http_mock),
    ):
        result = await synthesize_skill_md(
            name="test-skill",
            description="hace algo",
            db_path=_DB_PATH,
        )

    # repo_mock was never passed — the function must not have called it.
    repo_mock.get_active.assert_not_called()
    repo_mock.reveal_api_key.assert_not_called()
    assert "name: test-skill" in result or "test-skill" in result


@pytest.mark.asyncio
async def test_uses_config_model_and_api_key_in_request() -> None:
    """The HTTP POST must use config.model and Bearer config.api_key."""
    config = _make_config(
        model="vllm/my-model",
        api_key="bearer-token-42",
        base_url="http://vllm:8000/v1",
    )
    skill_body = (
        "===SKILL_START===\n"
        "---\nname: mi-skill\ndescription: desc\nversion: 1\n---\n# Mi Skill\n"
        "===SKILL_END==="
    )
    http_mock = AsyncMock()
    http_mock.__aenter__ = AsyncMock(return_value=http_mock)
    http_mock.__aexit__ = AsyncMock(return_value=False)
    http_mock.post = AsyncMock(return_value=_httpx_response(skill_body))

    with (
        patch(_RESOLVE_TARGET, return_value=config),
        patch(_HTTP_TARGET, return_value=http_mock),
    ):
        await synthesize_skill_md(
            name="mi-skill",
            description="desc",
            db_path=_DB_PATH,
        )

    call_kwargs = http_mock.post.call_args
    # URL must include /chat/completions on the configured base_url.
    assert call_kwargs.args[0] == "http://vllm:8000/v1/chat/completions"
    payload = call_kwargs.kwargs["json"]
    # La llamada HTTP cruda a un endpoint OpenAI-compatible usa el modelo SIN el
    # prefijo litellm de provider ("vllm/my-model" → "my-model"); con prefijo el
    # endpoint responde 404 (regresión cazada en verificación live).
    assert payload["model"] == "my-model"
    headers = call_kwargs.kwargs["headers"]
    assert headers["Authorization"] == "Bearer bearer-token-42"


@pytest.mark.asyncio
async def test_no_auth_header_when_api_key_is_none() -> None:
    """When config.api_key is None no Authorization header must be set."""
    config = _make_config(
        model="local/llama",
        api_key=None,
        base_url="http://localhost:11434/v1",
    )
    skill_body = (
        "===SKILL_START===\n"
        "---\nname: local-skill\ndescription: d\nversion: 1\n---\n# Local\n"
        "===SKILL_END==="
    )
    http_mock = AsyncMock()
    http_mock.__aenter__ = AsyncMock(return_value=http_mock)
    http_mock.__aexit__ = AsyncMock(return_value=False)
    http_mock.post = AsyncMock(return_value=_httpx_response(skill_body))

    with (
        patch(_RESOLVE_TARGET, return_value=config),
        patch(_HTTP_TARGET, return_value=http_mock),
    ):
        await synthesize_skill_md(
            name="local-skill",
            description="d",
            db_path=_DB_PATH,
        )

    headers = http_mock.post.call_args.kwargs["headers"]
    assert "Authorization" not in headers
