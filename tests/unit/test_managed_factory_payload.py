"""Actual engine factory kwargs; native SDK is the only fake in these unit tests."""

import asyncio
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from hermes.runtime import nous_engine
from hermes.runtime.model_config import ManagedProviderUnavailableError, ModelConfig
from tests.unit.test_nous_engine_hermes021_compat import _persona

pytestmark = pytest.mark.unit


def build(monkeypatch, config, *, substitute_gate=False):
    captured = {}
    from hermes.runtime import managed_llm_bootstrap

    monkeypatch.setattr(managed_llm_bootstrap, "assert_process_admission", lambda **_: None)
    if substitute_gate:
        monkeypatch.setattr(nous_engine, "_assert_managed_execution_ready", lambda _config: None)
    monkeypatch.setattr(nous_engine, "_cached_enrich_prompt", lambda prompt, _: prompt)
    monkeypatch.setattr(
        nous_engine,
        "_cached_resolve_hermes_runtime",
        lambda *_args: (
            {
                "api_key": config.api_key,
                "base_url": config.base_url,
                "provider": "custom",
                "api_mode": "chat_completions",
                "credential_pool": None,
            },
            "company",
        ),
    )

    def constructor(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(nous_engine, "_import_ai_agent", lambda: constructor)
    engine = nous_engine.NousReasoningEngine(persona=_persona())
    loop = asyncio.new_event_loop()
    try:
        engine._build_governed_agent(config, "Reply OK", loop, UUID(int=1))
    finally:
        loop.close()
    return captured


def test_real_factory_requires_process_admission_not_profile_flags(monkeypatch):
    with pytest.raises(ManagedProviderUnavailableError):
        build(
            monkeypatch,
            ModelConfig(
                model="custom/company",
                managed=True,
                extra={"managed_execution_ready": True, "allow_managed": True},
            ),
        )


@pytest.mark.parametrize("tokens", [None, 16, 4096, 4097])
def test_managed_factory_has_no_local_extra_body_and_forwards_explicit_tokens(monkeypatch, tokens):
    config = ModelConfig(
        model="custom/company",
        managed=True,
        api_key="fictional-scoped-grant",
        base_url="https://enterprise.test/v1/inference/g/v1",
        native_provider="custom",
        max_tokens=tokens,
    )
    kwargs = build(monkeypatch, config, substitute_gate=True)
    assert "request_overrides" not in kwargs
    assert kwargs["api_key"] == "fictional-scoped-grant"
    assert kwargs["base_url"] == config.base_url
    assert kwargs["model"] == "company"
    assert kwargs.get("max_tokens") == tokens


@pytest.mark.parametrize(
    "extra",
    [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        {"extra_body": {"model": "other", "max_tokens": 999999}},
        {"api_key": "other"},
        {"managed_execution_ready": True},
    ],
)
def test_managed_profile_or_tool_cannot_supply_request_overrides(monkeypatch, extra):
    with pytest.raises(ValueError, match="custom request overrides"):
        build(
            monkeypatch,
            ModelConfig(model="custom/company", managed=True, extra=extra),
            substitute_gate=True,
        )


@pytest.mark.parametrize("thinking", [None, True, False])
def test_local_qwen_body_behavior_is_preserved(monkeypatch, thinking):
    body = {} if thinking is None else {"chat_template_kwargs": {"enable_thinking": thinking}}
    kwargs = build(monkeypatch, ModelConfig(model="custom/qwen", extra={"extra_body": body}))
    assert kwargs["request_overrides"]["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": False if thinking is None else thinking
    }
