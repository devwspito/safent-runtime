"""Real signed envelope, existing pairing/vault, durable restart/revoke guards."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.config_sync.ads_policy_contract import AdsBindingSpec, AdsPolicySpec
from hermes.config_sync.policy_document import PolicyBundle, PolicyPayload, signing_bytes
from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.runtime.managed_ads_policy import (
    ManagedAdsUnavailable,
    apply_signed_ads,
    read_ads_policy,
)
from tests.unit.test_provider_active_governs_engine import _make_wiring

pytestmark = pytest.mark.unit


@pytest.fixture
def ads_policy(tmp_path):
    wiring = _make_wiring(tmp_path)
    store = SQLiteAssociationStore(
        db_path=wiring._provider_repo._db_path, vault=wiring._provider_repo._vault
    )
    key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC).isoformat()
    store.save(
        association=InstanceAssociation(
            instance_id="instance-a",
            tenant_id="org-a",
            paired_at=now,
            cloud_endpoint="https://enterprise.example",
            signing_pubkey_hex=key.public_key().public_bytes_raw().hex(),
            license={},
            last_applied_version=0,
            state="active",
        ),
        instance_secret="synthetic-pairing-value-only",
    )
    binding = AdsBindingSpec(
        grant_id="grant-a",
        revision=1,
        org_id="org-a",
        user_id="user-a",
        employee_id="employee-a",
        instance_id="instance-a",
        business_id="00000000-0000-4000-8000-000000000001",
        platform="meta",
        connection_id="00000000-0000-4000-8000-000000000002",
        external_account_id="123",
        resource_revision=1,
        role="ads",
        capabilities=["read", "propose", "approve", "execute"],
    )

    def envelope(version=1, mode="managed", bindings=None, **changes):
        spec = AdsPolicySpec(
            mode=mode,
            instance_id="instance-a",
            central_origin="https://ads.example" if mode == "managed" else None,
            bindings=([binding] if bindings is None else bindings) if mode == "managed" else [],
        )
        data = dict(
            version=version, tenant_id="org-a", issued_at=now, payload=PolicyPayload(ads=spec)
        )
        data.update(changes)
        return PolicyBundle(
            **data, signature_hex=key.sign(signing_bytes(**data)).hex()
        ).model_dump_json()

    return store, key, binding, envelope


def test_missing_policy_is_not_local_for_associated_instance(ads_policy, tmp_path):
    store, *_ = ads_policy
    assert read_ads_policy(tmp_path / "never-associated.sqlite") is None
    with pytest.raises(ManagedAdsUnavailable):
        read_ads_policy(store.db_path)


def test_signed_policy_restart_revoke_no_downgrade(ads_policy):
    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope())
    assert read_ads_policy(store.db_path).bindings[0].external_account_id == "123"
    apply_signed_ads(store, envelope(2, bindings=[]))
    assert read_ads_policy(store.db_path).bindings == []
    for candidate in (envelope(), envelope(3, mode="free")):
        with pytest.raises(ManagedAdsUnavailable):
            apply_signed_ads(store, candidate)


@pytest.mark.parametrize("change", ["signature", "instance", "tenant", "omit", "corrupt"])
def test_wrong_envelope_never_authorizes(ads_policy, change):
    store, _, _, envelope = ads_policy
    raw = json.loads(envelope())
    if change == "signature":
        raw["signature_hex"] = "0" * 128
    elif change == "instance":
        raw["payload"]["ads"]["instance_id"] = "other"
    elif change == "tenant":
        raw["tenant_id"] = "other"
    elif change == "omit":
        raw["payload"].pop("ads")
    with pytest.raises(ManagedAdsUnavailable):
        apply_signed_ads(store, "{" if change == "corrupt" else json.dumps(raw))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE instance_association SET state='revoked'",
        "UPDATE instance_association SET signing_pubkey_hex='untrusted'",
        "UPDATE instance_association SET last_applied_version=2",
        "DELETE FROM instance_association",
        "UPDATE runtime_ads_policy SET envelope='{}'",
    ],
)
def test_current_authority_changes_block_existing_routing(ads_policy, sql):
    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope())
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(sql)
    with pytest.raises(ManagedAdsUnavailable):
        read_ads_policy(store.db_path)


def test_signed_free_then_managed_never_returns_free(ads_policy):
    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope(mode="free"))
    assert read_ads_policy(store.db_path).mode == "free"
    apply_signed_ads(store, envelope(2))
    with pytest.raises(ManagedAdsUnavailable):
        apply_signed_ads(store, envelope(3, mode="free"))
