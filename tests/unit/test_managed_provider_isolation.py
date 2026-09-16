"""Selected LLM identity must survive the native adapter and shared workers."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import os

import pytest

from hermes.runtime import nous_engine as engine
from hermes.runtime.model_config import ModelConfig
from hermes.agents_os.infrastructure import dbus_runtime_service as dbus
from hermes.shell_server.providers.domain import ProviderKind

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_cache():
    engine.clear_runtime_provider_cache()
    yield
    engine.clear_runtime_provider_cache()


def test_explicit_assignment_beats_personal_native_config():
    resolver = MagicMock(return_value={'provider': 'anthropic'})
    with patch.dict('sys.modules', {
        'hermes_cli.config': SimpleNamespace(load_config=lambda: {'model': {'provider': 'openai-codex', 'default': 'personal-model'}}),
        'hermes_cli.runtime_provider': SimpleNamespace(resolve_runtime_provider=resolver),
    }), patch.object(engine, '_align_auxiliary_with_runtime'):
        _, bare = engine._resolve_hermes_runtime(ModelConfig(model='anthropic/enterprise-model', api_key='test-only'))
    assert bare == 'enterprise-model'
    assert resolver.call_args.kwargs['requested'] == 'anthropic'
    assert resolver.call_args.kwargs['explicit_api_key'] == 'test-only'


def test_native_oauth_uses_explicit_registry_id_without_api_key():
    resolver = MagicMock(return_value={'provider': 'openai-codex'})
    with patch.dict('sys.modules', {'hermes_cli.runtime_provider': SimpleNamespace(resolve_runtime_provider=resolver)}), patch.object(engine, '_align_auxiliary_with_runtime'):
        _, bare = engine._resolve_hermes_runtime(ModelConfig(model='openai-codex/native-model', native_provider='openai-codex'))
    assert bare == 'native-model'
    assert resolver.call_args.kwargs['requested'] == 'openai-codex'
    assert resolver.call_args.kwargs['explicit_api_key'] is None


@pytest.mark.parametrize('second', [
    ModelConfig(model='provider/model-b', api_key='token-a'),
    ModelConfig(model='provider/model-a', api_key='token-b'),
    ModelConfig(model='provider/model-a', api_key='token-a', base_url='https://other.invalid'),
])
def test_shared_engine_cache_binds_model_credentials_and_endpoint(second):
    with patch.object(engine, '_resolve_hermes_runtime', side_effect=lambda cfg: ({'test_identity': cfg}, cfg.model)) as resolve:
        first = ModelConfig(model='provider/model-a', api_key='token-a')
        engine._cached_resolve_hermes_runtime(9, first)
        result = engine._cached_resolve_hermes_runtime(9, second)
        assert result[0]['test_identity'] is second
        assert resolve.call_count == 2


def test_endpoint_clear_does_not_retain_previous_provider_url():
    save = MagicMock()
    with patch.dict('sys.modules', {'hermes_cli.config': SimpleNamespace(load_config=lambda: {'model': {'base_url': 'https://old.invalid'}}, save_config=save)}):
        dbus._write_hermes_model_config('anthropic', 'new-model', '')
    assert 'base_url' not in save.call_args.args[0]['model']


def test_inactive_alias_never_overwrites_active_environment():
    wiring = object.__new__(dbus.DbusRuntimeServiceWiring)
    wiring._active_provider_svc = None
    provider = SimpleNamespace(kind=ProviderKind.OPENAI, default_model='new-model', base_url=None)
    with patch.dict('sys.modules', {'hermes_cli.auth': SimpleNamespace(PROVIDER_REGISTRY={})}), patch.object(dbus, '_write_hermes_env') as env, patch.object(dbus, '_write_hermes_model_config') as model, patch.dict(os.environ, {'OPENAI_API_KEY':'test-active'}):
        wiring._sync_to_native_provider(provider, 'test-inactive', set_active=False)
        assert os.environ['OPENAI_API_KEY'] == 'test-active'
        env.assert_not_called()
        model.assert_not_called()


def test_keyless_local_selection_still_updates_native_model():
    wiring = object.__new__(dbus.DbusRuntimeServiceWiring)
    wiring._active_provider_svc = None
    provider = SimpleNamespace(kind=ProviderKind.NOUS, default_model='oauth-model', base_url=None)
    with patch.dict('sys.modules', {'hermes_cli.auth': SimpleNamespace(PROVIDER_REGISTRY={})}), patch.object(dbus, '_write_hermes_env') as env, patch.object(dbus, '_write_hermes_model_config') as model, patch.object(dbus, '_clear_engine_runtime_cache'):
        wiring._sync_to_native_provider(provider, None, set_active=True)
        model.assert_called_once_with('nous', 'oauth-model', '')
        env.assert_not_called()


def test_managed_credentials_cannot_fall_back_to_local_oauth_or_environment():
    resolver = MagicMock()
    with patch.dict('sys.modules', {'hermes_cli.runtime_provider': SimpleNamespace(resolve_runtime_provider=resolver)}):
        with pytest.raises(RuntimeError, match='Managed provider'):
            engine._resolve_hermes_runtime(ModelConfig(model='openai/model', base_url='https://enterprise.invalid/v1', managed=True))
    resolver.assert_not_called()


def test_model_config_repr_never_discloses_credentials():
    assert 'test-secret' not in repr(ModelConfig(model='model', api_key='test-secret'))


def test_new_managed_policy_overrides_engine_personal_snapshot():
    instance = object.__new__(engine.NousReasoningEngine)
    instance._agent_registry = None
    instance._model_config = ModelConfig(model='personal/model')
    instance._model_config_source = lambda: current[0]
    current = [None]
    assert instance._resolve_model_config() is instance._model_config
    current[0] = ModelConfig(model='custom/corporate', managed=True)
    assert instance._resolve_model_config() is current[0]


def test_unadmitted_managed_execution_fails_before_native_construction():
    instance = object.__new__(engine.NousReasoningEngine)
    with patch.object(engine, 'GovernedAIAgent') as native:
        with pytest.raises(RuntimeError, match='corporate bootstrap'):
            instance._build_governed_agent(ModelConfig(model='custom/corporate', managed=True), '', None, None)
    native.assert_not_called()


def test_local_execution_still_constructs_governed_native_agent():
    from hermes.agents.domain.agent import default_agent
    from uuid import UUID
    instance = engine.NousReasoningEngine(persona=default_agent().to_persona())
    with patch.object(engine, 'GovernedAIAgent') as native, \
            patch.object(engine, '_cached_enrich_prompt', return_value='test'), \
            patch.object(engine, '_cached_resolve_hermes_runtime', return_value=({'provider': 'openai-codex'}, 'personal')):
        instance._build_governed_agent(ModelConfig(model='openai-codex/personal'), '', None, UUID(int=0))
    native.assert_called_once()


def test_concurrent_native_cache_keeps_separate_selected_credentials():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    gate = Barrier(2)
    configs = [ModelConfig(model='custom/a', api_key='test-a'), ModelConfig(model='custom/b', api_key='test-b')]
    def resolve(cfg):
        gate.wait(timeout=3)
        return {'api_key': cfg.api_key}, cfg.model
    with patch.object(engine, '_resolve_hermes_runtime', side_effect=resolve), ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda cfg: engine._cached_resolve_hermes_runtime(1, cfg), configs))
    assert results == [({'api_key': 'test-a'}, 'custom/a'), ({'api_key': 'test-b'}, 'custom/b')]
