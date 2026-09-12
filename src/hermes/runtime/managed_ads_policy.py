"""Fail-closed Ads selection from the current association's signed policy.

No OAuth, delegation token or local override is persisted here. A missing
policy is local only for a never-associated, never-managed installation.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import cast

from hermes.config_sync.ads_policy_contract import AdsPolicySpec
from hermes.config_sync.policy_document import PolicyBundle, signing_bytes
from hermes.config_sync.signature import verify_bundle
from hermes.instance.association_store import SQLiteAssociationStore
from hermes.security.configuration_lock import configuration_lock

MAX_ENVELOPE = 2_000_000
type AssociationRow = tuple[str, str, str, str, str, int]


class ManagedAdsUnavailable(PermissionError):
    def __init__(self) -> None:
        super().__init__("Ads policy unavailable; local fallback is forbidden")


def _parse(raw: str) -> PolicyBundle:
    try:
        if len(raw.encode()) > MAX_ENVELOPE:
            raise ValueError
        return PolicyBundle.model_validate_json(raw)
    except (ValueError, TypeError, UnicodeError):
        raise ManagedAdsUnavailable() from None


def _association(conn: sqlite3.Connection) -> AssociationRow | None:
    return cast(
        AssociationRow | None,
        conn.execute(
            "SELECT instance_id, tenant_id, state, signing_pubkey_hex, cloud_endpoint, "
            "last_applied_version FROM instance_association WHERE id=1"
        ).fetchone(),
    )


def _fingerprint(association: AssociationRow) -> str:
    return hashlib.sha256(repr(association[:5]).encode()).hexdigest()


def _verify(bundle: PolicyBundle, association: AssociationRow | None) -> AdsPolicySpec:
    spec = bundle.payload.ads
    if (
        not association
        or association[2] != "active"
        or spec is None
        or bundle.tenant_id != association[1]
        or spec.instance_id != association[0]
        or any(binding.org_id != association[1] for binding in spec.bindings)
        or not verify_bundle(
            payload_canonical=signing_bytes(
                version=bundle.version,
                tenant_id=bundle.tenant_id,
                issued_at=bundle.issued_at,
                payload=bundle.payload,
            ),
            signature_hex=bundle.signature_hex,
            pubkey_hex=association[3],
        )
    ):
        raise ManagedAdsUnavailable()
    return spec


def _state(conn: sqlite3.Connection) -> tuple[str, str] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_ads_policy'"
    ).fetchone()
    return (
        conn.execute(
            "SELECT envelope, trust_fingerprint FROM runtime_ads_policy WHERE id=1"
        ).fetchone()
        if exists
        else None
    )


def read_ads_policy(db_path: Path) -> AdsPolicySpec | None:
    """None means never associated. All ambiguous states deny before local IO."""
    if not db_path.exists():
        return None
    try:
        with (
            configuration_lock(db_path),
            sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as conn,
        ):
            state = _state(conn)
            associated = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instance_association'"
            ).fetchone()
            association = _association(conn) if associated else None
            if state is None:
                if association is not None:
                    raise ManagedAdsUnavailable()
                return None
            bundle = _parse(state[0])
            if association is None:
                raise ManagedAdsUnavailable()
            spec = _verify(bundle, association)
            if state[1] != _fingerprint(association) or bundle.version < association[5]:
                raise ManagedAdsUnavailable()
            return spec
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise ManagedAdsUnavailable() from None


def apply_signed_ads(store: SQLiteAssociationStore, envelope: str) -> dict[str, object]:
    # Reuse the config-sync freshness policy; do not invent a second TTL.
    from hermes.config_sync.__main__ import _check_freshness  # noqa: PLC0415

    try:
        with configuration_lock(store.db_path), sqlite3.connect(store.db_path) as conn:
            bundle = _parse(envelope)
            association = _association(conn)
            if association is None:
                raise ManagedAdsUnavailable()
            spec = _verify(bundle, association)
            if not _check_freshness(bundle.issued_at) or bundle.version < association[5]:
                raise ManagedAdsUnavailable()
            previous = _state(conn)
            if previous:
                old = _parse(previous[0])
                if (
                    bundle.version < old.version
                    or (bundle.version == old.version and bundle != old)
                    or (
                        old.payload.ads is not None
                        and old.payload.ads.mode == "managed"
                        and spec.mode == "free"
                    )
                ):
                    raise ManagedAdsUnavailable()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_ads_policy "
                "(id INTEGER PRIMARY KEY CHECK(id=1), envelope TEXT NOT NULL, "
                "trust_fingerprint TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO runtime_ads_policy VALUES (1, ?, ?)",
                (envelope, _fingerprint(association)),
            )
            return {"ok": True, "mode": spec.mode, "version": bundle.version}
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise ManagedAdsUnavailable() from None
