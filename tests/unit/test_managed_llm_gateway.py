"""Signed daemon boundary + real SQLite/vault, never a live provider/account."""
from datetime import UTC, datetime
from unittest.mock import patch
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.config_sync.policy_document import PolicyBundle, PolicyPayload, ProviderSpec, signing_bytes
from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.runtime.managed_llm import read_policy, _resolve_managed_binding as resolve_managed_config
from hermes.runtime.model_config import ManagedProviderUnavailableError
from tests.unit.test_provider_active_governs_engine import _make_wiring

pytestmark = pytest.mark.unit


@pytest.fixture
def setup(tmp_path, monkeypatch):
    wiring = _make_wiring(tmp_path)
    repo = wiring._provider_repo
    from hermes.shell_server.security import secrets
    monkeypatch.setattr(secrets, 'SecretsVault', lambda: repo._vault)
    store = SQLiteAssociationStore(db_path=repo._db_path, vault=repo._vault)
    private = Ed25519PrivateKey.generate()
    now = datetime.now(UTC).isoformat()
    store.save(association=InstanceAssociation(instance_id='instance-a', tenant_id='org-a', paired_at=now,
        cloud_endpoint='https://enterprise.example', signing_pubkey_hex=private.public_key().public_bytes_raw().hex(),
        license={}, last_applied_version=0, state='active'), instance_secret='test-pairing-not-inference')
    wiring._association_store = store

    def bundle(version=1, *, providers=True, **changes):
        spec = ProviderSpec(alias='company', kind='openai_compatible', default_model='allowed-model',
            base_url='https://enterprise.example/v1/inference/grant-a/v1', api_key='test-scoped-inference',
            credential_kind='instance_gateway', set_active=True)
        payload = PolicyPayload(llm_instance_id='instance-a', providers=[spec] if providers else [])
        data = dict(version=version, tenant_id='org-a', issued_at=now, payload=payload)
        data.update(changes)
        signature = private.sign(signing_bytes(**data)).hex()
        return PolicyBundle(**data, signature_hex=signature).model_dump_json()
    return wiring, bundle


def test_signed_gateway_resolves_vault_token_without_exporting_to_native_env(setup):
    wiring, bundle = setup
    with patch('hermes.agents_os.infrastructure.dbus_runtime_service._write_hermes_env') as write:
        result = wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    write.assert_not_called()
    assert result['status'] == 'active'
    config = resolve_managed_config(wiring._provider_repo._db_path)
    assert config.managed and config.native_provider == 'custom'
    assert config.api_key == 'test-scoped-inference'
    assert config.model == 'custom/allowed-model'
    assert 'test-scoped' not in json.dumps(read_policy(wiring._provider_repo._db_path))


def test_managed_default_and_unknown_alias_never_fall_back(setup):
    wiring, bundle = setup
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(wiring._provider_repo._db_path, 'personal')
    provider = wiring._provider_repo.list_all()[0]
    provider.enabled = False
    wiring._provider_repo.update(provider=provider)
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(wiring._provider_repo._db_path)


def test_revoke_tombstone_blocks_replay_native_switch_and_survives_restart(setup):
    wiring, bundle = setup
    original = bundle()
    wiring.apply_managed_llm_gateway(bundle_json=original, sender_uid=1000)
    wiring.apply_managed_llm_gateway(bundle_json=bundle(2, providers=False), sender_uid=1000)
    assert wiring._provider_repo.list_all() == []
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(wiring._provider_repo._db_path)
    with pytest.raises(PermissionError, match='rollback'):
        wiring.apply_managed_llm_gateway(bundle_json=original, sender_uid=1000)
    with pytest.raises(PermissionError, match='managed'):
        wiring.set_active_provider(provider_id='openai-codex', sender_uid=1000)
    assert read_policy(wiring._provider_repo._db_path)['status'] == 'revoked'


def test_verified_same_version_is_idempotent_not_reapplied(setup):
    wiring, bundle = setup
    original = bundle()
    wiring.apply_managed_llm_gateway(bundle_json=original, sender_uid=1000)
    with patch.object(wiring._provider_repo, 'update') as update:
        result = wiring.apply_managed_llm_gateway(bundle_json=original, sender_uid=1000)
    assert result['unchanged']
    update.assert_not_called()


@pytest.mark.parametrize('change', ['signature', 'instance', 'tenant', 'direct', 'endpoint'])
def test_wrong_authority_never_writes_a_provider(setup, change):
    wiring, bundle = setup
    data = json.loads(bundle())
    if change == 'signature': data['signature_hex'] = '0' * 128
    elif change == 'instance': data['payload']['llm_instance_id'] = 'instance-b'
    elif change == 'tenant': data['tenant_id'] = 'org-b'
    elif change == 'direct': data['payload']['providers'][0]['credential_kind'] = 'direct'
    else: data['payload']['providers'][0]['base_url'] = 'https://attacker.example/v1/inference/grant/v1'
    with pytest.raises(PermissionError):
        wiring.apply_managed_llm_gateway(bundle_json=json.dumps(data), sender_uid=1000)
    assert wiring._provider_repo.list_all() == []


def test_signed_wrong_instance_and_legacy_direct_are_rejected(setup):
    wiring, bundle = setup
    for payload in [PolicyPayload(llm_instance_id='other'), PolicyPayload(llm_instance_id='instance-a', providers=[ProviderSpec(alias='master', kind='openai', default_model='model', api_key='upstream-master-test')])]:
        with pytest.raises(PermissionError):
            wiring.apply_managed_llm_gateway(bundle_json=bundle(payload=payload), sender_uid=1000)
    assert wiring._provider_repo.list_all() == []


def test_all_local_mutators_denied_in_managed_mode_and_explicit_unpair_restores_free_mode(setup):
    wiring, bundle = setup
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    actions = [lambda: wiring.add_provider(draft_json='{}', sender_uid=1000),
        lambda: wiring.configure_native_provider(provider_id='openai-api', api_key='test', model='test', base_url='', sender_uid=1000),
        lambda: wiring.start_provider_oauth(provider_id='openai-codex', sender_uid=1000)]
    for action in actions:
        with pytest.raises(PermissionError): action()
    wiring._association_store.clear()
    assert resolve_managed_config(wiring._provider_repo._db_path) is None
    wiring._reject_local_llm_mutation()


def test_revoked_association_does_not_silently_end_management(setup):
    wiring, bundle = setup
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    wiring._association_store.mark_revoked()
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(wiring._provider_repo._db_path)


def test_legacy_wire_defaults_remain_absent():
    assert 'llm_instance_id' not in PolicyPayload().model_dump()
    assert 'credential_kind' not in ProviderSpec(alias='local', kind='openai', default_model='model').model_dump()


def test_signed_policy_supports_deployment_prefix_and_rejects_other_paths(setup):
    from dataclasses import replace
    wiring, bundle = setup
    store = wiring._association_store
    store.save(association=replace(store.get(), cloud_endpoint='https://enterprise.example/company'),
               instance_secret='test-pairing-not-inference')
    def payload(path):
        return PolicyPayload(llm_instance_id='instance-a', providers=[ProviderSpec(
            alias='company', kind='openai_compatible', default_model='allowed-model',
            base_url='https://enterprise.example' + path, api_key='test-scoped',
            credential_kind='instance_gateway', set_active=True)])
    for path in ['/v1/inference/grant/v1', '/company/v1/inference/../v1', '/company/v1/inference/x/y/v1']:
        with pytest.raises(PermissionError):
            wiring.apply_managed_llm_gateway(bundle_json=bundle(payload=payload(path)), sender_uid=1000)
    wiring.apply_managed_llm_gateway(bundle_json=bundle(payload=payload('/company/v1/inference/grant/v1')), sender_uid=1000)
    assert resolve_managed_config(store.db_path).base_url.endswith('/company/v1/inference/grant/v1')


def test_expired_policy_and_same_version_mutation_are_rejected(setup):
    wiring, bundle = setup
    with pytest.raises(PermissionError, match='expired'):
        wiring.apply_managed_llm_gateway(bundle_json=bundle(issued_at='2020-01-01T00:00:00+00:00'), sender_uid=1000)
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    with pytest.raises(PermissionError, match='version collision'):
        wiring.apply_managed_llm_gateway(bundle_json=bundle(providers=False), sender_uid=1000)


def test_failed_vault_write_stays_blocked_and_same_signed_policy_can_retry(setup):
    wiring, bundle = setup
    signed = bundle()
    with patch.object(wiring._provider_repo, 'add', side_effect=RuntimeError('test vault unavailable')):
        with pytest.raises(RuntimeError):
            wiring.apply_managed_llm_gateway(bundle_json=signed, sender_uid=1000)
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(wiring._provider_repo._db_path)
    wiring.apply_managed_llm_gateway(bundle_json=signed, sender_uid=1000)
    assert resolve_managed_config(wiring._provider_repo._db_path).managed


def test_all_production_config_sources_refuse_token_release_without_admitted_process(setup):
    from hermes.runtime.managed_llm import resolve_managed_config as production_source
    from hermes.runtime.provider_config_source import resolve_model_config
    from hermes.runtime.active_provider import ActiveProviderService
    wiring, bundle = setup
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    path = wiring._provider_repo._db_path
    for source in [lambda: production_source(path), lambda: resolve_model_config(path),
                   lambda: ActiveProviderService(path).resolve()]:
        with patch.object(wiring._provider_repo, 'reveal_api_key') as reveal:
            with pytest.raises(ManagedProviderUnavailableError, match='corporate bootstrap'):
                source()
            reveal.assert_not_called()
