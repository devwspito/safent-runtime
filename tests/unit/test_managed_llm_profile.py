"""LLM-02A: isolated profile construction, never a live profile or account."""
from copy import deepcopy
from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.config_sync.policy_document import PolicyBundle
from tests.unit.test_managed_llm_gateway import setup  # noqa: F401
from hermes.agents_os.infrastructure import dbus_runtime_service as dbus
from hermes.runtime.model_config import ModelConfig

pytestmark = pytest.mark.unit


def test_second_signed_binding_is_rejected_before_any_state_write(setup):
    wiring, signed = setup
    payload = PolicyBundle.model_validate_json(signed()).payload
    payload.providers.append(payload.providers[0].model_copy(update={'alias': 'second', 'set_active': False}))
    with pytest.raises(PermissionError, match='one.*binding'):
        wiring.apply_managed_llm_gateway(bundle_json=signed(payload=payload), sender_uid=1000)
    assert wiring._provider_repo.list_all() == []


def test_late_nous_oauth_cannot_persist_after_enterprise_assignment(setup):
    import time
    wiring, signed = setup
    sid = 'test-late-oauth'
    dbus._OAUTH_SESSIONS[sid] = {'status': 'pending', 'expires_at': time.time()+60,
        'portal_base_url': 'https://oauth.invalid', 'client_id': 'test-client',
        'device_code': 'test-code', 'interval': 1,
        'llm_db_path': str(wiring._provider_repo._db_path)}
    save = MagicMock()
    def token_arrives(**kwargs):
        wiring.apply_managed_llm_gateway(bundle_json=signed(), sender_uid=1000)
        return {'access_token': 'test-personal-oauth'}
    with patch.dict('sys.modules', {'hermes_cli.auth': SimpleNamespace(
        _poll_for_token=token_arrives, persist_nous_credentials=save,
        refresh_nous_oauth_from_state=lambda value, **kwargs: value)}), \
        patch.object(dbus, '_write_hermes_model_config') as model:
        dbus._nous_oauth_poller(sid)
    save.assert_not_called()
    model.assert_not_called()
    assert dbus._OAUTH_SESSIONS.pop(sid)['status'] == 'error'


@pytest.mark.asyncio
async def test_future_enabled_managed_source_never_autowires_mcp_environment():
    managed = ModelConfig(model='custom/company', managed=True,
        api_key='test-scoped-only', base_url='https://enterprise.invalid/v1')
    manager = SimpleNamespace(connect=AsyncMock())
    with patch('hermes.runtime.active_provider.ActiveProviderService.resolve', return_value=managed):
        await dbus._mcp_connect(manager, 'test-mcp', ['test'], env={'OPENAI_API_KEY': '', 'OPENAI_BASE_URL': ''})
    transport = manager.connect.call_args.kwargs['transport']
    assert 'test-scoped-only' not in repr(transport)
    assert not transport.env.get('OPENAI_API_KEY')


def test_profile_drops_personal_fallbacks_and_pins_every_native_aux_task():
    from hermes.runtime.managed_llm_profile import build_profile
    defaults = {'model': {'default': 'personal', 'provider': 'openrouter'},
        'auxiliary': {'compression': {'provider': 'auto'}, 'future_task': {'provider': 'openrouter', 'fallback_chain': [{'provider': 'personal'}]}},
        'custom_providers': [{'name': 'personal'}], 'fallback_providers': ['personal'],
        'mcp_servers': {'personal': {}}, 'plugins': {'personal': {'enabled': True}}}
    before = deepcopy(defaults)
    binding = ModelConfig(model='custom/company', managed=True, native_provider='custom',
        api_key='test-scoped-only', base_url='https://enterprise.invalid/v1/inference/grant/v1')
    profile = build_profile(binding, defaults)
    assert defaults == before
    assert profile['model']['provider'] == 'custom'
    assert profile['model']['default'] == 'company'
    assert profile['fallback_providers'] == []
    assert profile['custom_providers'] == []
    assert not profile.get('mcp_servers') and not profile.get('plugins')
    for task in profile['auxiliary'].values():
        if isinstance(task, dict):
            assert task['provider'] == 'custom' and task['model'] == 'company'
            assert task['base_url'] == binding.base_url
            assert task['fallback_chain'] == []
    assert 'personal' not in json.dumps(profile)
    assert 'test-scoped-only' not in json.dumps(profile)


def test_profile_environment_is_allowlisted_not_inherited():
    from hermes.runtime.managed_llm_profile import allowed_environment
    actual = allowed_environment({'PATH': '/unsafe/personal', 'LANG': 'es_ES.UTF-8',
        'OPENAI_API_KEY': 'test-personal', 'HERMES_MODEL': 'personal', 'PYTHONPATH': '/personal',
        'HTTP_PROXY': 'https://proxy.invalid', 'AWS_PROFILE': 'personal'}, Path('/tmp/company-profile'))
    assert actual['HERMES_HOME'] == actual['HOME'] == '/tmp/company-profile'
    assert actual['PATH'] == '/usr/local/bin:/usr/bin:/bin'
    assert actual['LANG'] == 'es_ES.UTF-8'
    assert not any(name in actual for name in ['OPENAI_API_KEY', 'HERMES_MODEL', 'PYTHONPATH', 'HTTP_PROXY', 'AWS_PROFILE'])


@pytest.mark.parametrize('change', [
    {'managed': False}, {'native_provider': 'openai'}, {'model': 'custom/'},
    {'api_key': ''}, {'base_url': 'http://enterprise.invalid/v1'},
    {'base_url': 'https://user:secret@enterprise.invalid/v1'},
    {'base_url': 'https://enterprise.invalid/v1?key=secret'},
])
def test_profile_rejects_unverified_or_unsafe_binding(change):
    from hermes.runtime.managed_llm_profile import build_profile
    values = dict(model='custom/company', managed=True, native_provider='custom',
        api_key='test-only', base_url='https://enterprise.invalid/v1')
    with pytest.raises(ValueError):
        build_profile(ModelConfig(**(values | change)), {'auxiliary': {'compression': {}}})


def test_profile_requires_native_task_catalog_and_absolute_home():
    from hermes.runtime.managed_llm_profile import build_profile, allowed_environment
    binding = ModelConfig(model='custom/company', managed=True, native_provider='custom',
        api_key='test-only', base_url='https://enterprise.invalid/v1')
    for defaults in ({}, {'auxiliary': []}, {'auxiliary': {'transient_retries': 2}}):
        with pytest.raises(ValueError):
            build_profile(binding, defaults)
    with pytest.raises(ValueError):
        allowed_environment({}, Path('relative'))


def test_signed_assignment_waits_for_local_setter_commit(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from hermes.runtime.managed_llm import read_policy
    wiring, signed = setup
    local_commit, release_commit, apply_started = Event(), Event(), Event()
    envelope = signed()
    def slow_native_sync(*args, **kwargs):
        local_commit.set()
        assert release_commit.wait(5)
        assert read_policy(wiring._provider_repo._db_path) is None
    def assign():
        apply_started.set()
        return wiring.apply_managed_llm_gateway(bundle_json=envelope, sender_uid=1000)
    with patch.object(wiring, '_sync_to_native_provider', side_effect=slow_native_sync), \
            ThreadPoolExecutor(max_workers=2) as pool:
        local = pool.submit(wiring.add_provider, draft_json=json.dumps({
            'alias':'local', 'kind':'openai', 'default_model':'local', 'set_active':True,
        }), sender_uid=1000)
        assert local_commit.wait(5)
        managed = pool.submit(assign)
        assert apply_started.wait(5)
        try:
            assert not managed.done()
            assert read_policy(wiring._provider_repo._db_path) is None
        finally:
            release_commit.set()
        local.result(timeout=5)
        assert managed.result(timeout=5)['status'] == 'active'
    with pytest.raises(PermissionError, match='managed'):
        wiring.add_provider(draft_json='{}', sender_uid=1000)


def test_oauth_callback_without_captured_scope_never_commits():
    sid = 'missing-scope'
    dbus._OAUTH_SESSIONS[sid] = {'status': 'pending'}
    try:
        with pytest.raises(PermissionError, match='verified'):
            with dbus._oauth_local_commit(sid):
                pytest.fail('Unscoped callback committed')
    finally:
        dbus._OAUTH_SESSIONS.pop(sid)


@pytest.mark.parametrize('provider', ['nous', 'openai-codex', 'xai-oauth'])
@pytest.mark.parametrize('managed', [False, True])
def test_oauth_commit_checks_current_authority_for_every_supported_flow(setup, provider, managed):
    import time
    wiring, signed = setup
    sid = 'test-oauth-' + provider
    dbus._OAUTH_SESSIONS[sid] = {'status':'pending', 'expires_at':time.time()+60,
        'portal_base_url':'https://oauth.invalid', 'client_id':'test-client', 'device_code':'test-code',
        'interval':1, 'llm_db_path':str(wiring._provider_repo._db_path),
        'server':None, 'thread':None, 'callback_result':None, 'state':'test-state',
        'token_endpoint':'https://oauth.invalid/token', 'redirect_uri':'http://localhost/callback',
        'verifier':'test-verifier', 'challenge':'test-challenge'}
    save = MagicMock()
    def token_arrives(**kwargs):
        if managed:
            wiring.apply_managed_llm_gateway(bundle_json=signed(), sender_uid=1000)
        return {'access_token':'test-personal-oauth', 'refresh_token':'test-refresh'}
    auth = SimpleNamespace(_poll_for_token=token_arrives, persist_nous_credentials=save,
        refresh_nous_oauth_from_state=lambda value, **kwargs:value,
        CODEX_OAUTH_CLIENT_ID='test-client', CODEX_OAUTH_TOKEN_URL='https://oauth.invalid/token',
        _save_codex_tokens=save, _save_xai_oauth_tokens=save,
        _xai_wait_for_callback=lambda *args, **kwargs:{'code':'test-code', 'state':'test-state'},
        _xai_oauth_exchange_code_for_tokens=token_arrives)
    def post(url, **kwargs):
        if url.endswith('/usercode'):
            data = {'user_code':'test-user', 'device_auth_id':'test-device', 'interval':'3'}
        elif url.endswith('/deviceauth/token'):
            data = {'authorization_code':'test-code','code_verifier':'test-verifier'}
        else:
            data = token_arrives()
        return SimpleNamespace(status_code=200, json=lambda:data)
    client = MagicMock()
    client.__enter__.return_value.post.side_effect = post
    flows = {'nous':dbus._nous_oauth_poller, 'openai-codex':dbus._codex_oauth_worker, 'xai-oauth':dbus._xai_loopback_worker}
    with patch.dict('sys.modules', {'hermes_cli':SimpleNamespace(auth=auth), 'hermes_cli.auth':auth}), \
            patch('httpx.Client', return_value=client), patch('time.sleep'), \
            patch.object(dbus, '_write_hermes_model_config') as model:
        flows[provider](sid)
    state = dbus._OAUTH_SESSIONS.pop(sid)
    if managed:
        save.assert_not_called(); model.assert_not_called()
        assert state['status'] == 'error'
        assert 'managed by Enterprise' in state['error_message']
    else:
        save.assert_called_once(); model.assert_called_once()
        assert state['status'] == 'approved'
