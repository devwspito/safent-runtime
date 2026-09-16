"""Probe uses the native Responses adapter, not a raw chat/completions call."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes.providers.infrastructure.native_probe import probe_responses_runtime

pytestmark = pytest.mark.unit


@pytest.fixture
def native(monkeypatch):
    client = MagicMock()
    resolver = MagicMock(return_value=(client, "gpt-6-astra"))
    module = ModuleType("agent.auxiliary_client")
    module.resolve_provider_client = resolver
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    return resolver, client


def runtime():
    return {
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "api_key": "test-token",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }


def test_probe_pins_native_responses_transport_without_tools_or_local_parameters(native):
    resolver, client = native
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
    )
    assert probe_responses_runtime(runtime(), "gpt-6-astra") == (True, None)
    resolver.assert_called_once_with(
        provider="openai-codex",
        model="gpt-6-astra",
        explicit_api_key="test-token",
        explicit_base_url=runtime()["base_url"],
        api_mode="codex_responses",
        raw_codex=False,
    )
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-6-astra"
    assert kwargs["timeout"] == 20
    assert "tools" not in kwargs and "extra_body" not in kwargs
    client.close.assert_called_once()


@pytest.mark.parametrize(
    "choices",
    [
        [],
        [SimpleNamespace(message=SimpleNamespace(content=""))],
        [SimpleNamespace(message=SimpleNamespace(content=None))],
    ],
)
def test_empty_response_never_means_connection_verified(native, choices):
    _, client = native
    client.chat.completions.create.return_value = SimpleNamespace(choices=choices)
    assert probe_responses_runtime(runtime(), "gpt-6-astra")[0] is False
    client.close.assert_called_once()


def test_provider_http_error_propagates_for_classification_without_retry_or_fallback(native):
    resolver, client = native
    client.chat.completions.create.side_effect = RuntimeError("upstream failure")
    with pytest.raises(RuntimeError):
        probe_responses_runtime(runtime(), "gpt-6-astra")
    assert resolver.call_count == 1
    assert client.chat.completions.create.call_count == 1
    client.close.assert_called_once()


@pytest.mark.parametrize("override", [{"provider": "auto"}, {"api_mode": "chat_completions"}])
def test_probe_never_auto_selects_a_different_account(native, override):
    with pytest.raises(ValueError):
        probe_responses_runtime({**runtime(), **override}, "gpt-6-astra")
    native[0].assert_not_called()


def test_missing_native_client_is_a_failure(native):
    native[0].return_value = (None, None)
    assert probe_responses_runtime(runtime(), "gpt-6-astra")[0] is False
