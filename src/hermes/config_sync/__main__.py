"""hermes.config_sync — periodic cloud→associate policy sync loop.

Run with: python3 -m hermes.config_sync

Sync tick order (P0-2 — verify-first):
  1. GET {cloud_endpoint}/v1/policy?instance_id=...
     Authorization: Bearer {instance_secret}
  2. Size-cap body (P1-3) and parse to PolicyBundle.
  3. Validate pubkey_hex from association (P2 — must be 64 hex chars).
  4. Verify Ed25519 signature over the FULL ENVELOPE (P0-1, fail-closed).
     Only after a valid signature do we trust ANY field in the bundle.
  5. Check tenant_id matches association (now over a signed value).
  6. Check issued_at freshness: reject if > now+5min or < now-30d (P1-1).
  7. Anti-rollback: skip if bundle.version <= last_applied_version.
  8. Apply via PolicyApplier.
  9. On full success: advance last_applied_version + persist license.

Transport note (Fase 4):
    Currently uses Bearer token over TLS (HTTPS-only, SSRF-protected).
    Fase 7 injection point (mTLS): add cert=(<cert>, <key>) to httpx.get().
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from hermes.config_sync.applier import PolicyApplier
from hermes.config_sync.policy_document import PolicyBundle, signing_bytes
from hermes.config_sync.signature import verify_bundle
from hermes.config_sync.uploader import UsageUploader
from hermes.instance.association_store import SQLiteAssociationStore
from hermes.instance.infrastructure.http_control_plane_client import (
    _validate_cloud_endpoint,
)
from hermes.instance.pairing_service import PairingError
from hermes.shell_server.cowork.dbus_proxy import DbusRuntimeProxy
from hermes.shell_server.metering.usage_repo import SQLiteUsageRepository
from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger("hermes.config_sync")

_DEFAULT_INTERVAL_S = 300
_MAX_JITTER_S = 30
_HTTP_TIMEOUT_S = 20.0

# P1-1: freshness window tuneable via env.
_FRESHNESS_FUTURE_S = int(os.environ.get("HERMES_SYNC_FRESHNESS_FUTURE_S", "300"))   # +5 min
_FRESHNESS_PAST_S = int(os.environ.get("HERMES_SYNC_FRESHNESS_PAST_S", "2592000"))   # -30 days

# P1-3: max response body size (bytes) to parse.
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB

# P2: expected pubkey_hex length (Ed25519 public key = 32 bytes = 64 hex chars).
_PUBKEY_HEX_LEN = 64

# Single source of truth: usage_events + association both live in shell-state.db
# (the daemon writes usage via SQLiteUsageRepository(db_path=HERMES_SHELL_DB) and
# the metering API reads the same file). Honor HERMES_SHELL_DB — the canonical var
# the daemon uses and pre-creates at boot — falling back to HERMES_STATE_DB then
# the default. A separate usage.db would read an empty DB (the uploader bug fix).
_STATE_DB_PATH = Path(
    os.environ.get(
        "HERMES_SHELL_DB",
        os.environ.get("HERMES_STATE_DB", "/var/lib/hermes/shell-state.db"),
    )
)


def _build_store() -> SQLiteAssociationStore:
    vault = SecretsVault()
    return SQLiteAssociationStore(db_path=_STATE_DB_PATH, vault=vault)


def _build_uploader(store: SQLiteAssociationStore) -> UsageUploader:
    # usage_events lives in shell-state.db (same file the daemon writes + the
    # metering API reads) — NOT a separate usage.db.
    usage_repo = SQLiteUsageRepository(db_path=_STATE_DB_PATH)
    return UsageUploader(usage_repo=usage_repo, association_store=store)


def _interval_s() -> int:
    raw = os.environ.get("HERMES_CONFIG_SYNC_INTERVAL", "")
    try:
        val = int(raw)
        return max(30, val)
    except (ValueError, TypeError):
        return _DEFAULT_INTERVAL_S


def _policy_url(cloud_endpoint: str, instance_id: str, applied_version: int = 0) -> str:
    # applied_version is the heartbeat: on each poll we tell the cloud which
    # version we currently have applied, so the Fleet view shows real convergence
    # (published vs applied), not just what was published.
    return (
        f"{cloud_endpoint.rstrip('/')}/v1/policy"
        f"?instance_id={instance_id}&applied_version={applied_version}"
    )


def _endpoint_is_safe(endpoint: str) -> bool:
    try:
        _validate_cloud_endpoint(endpoint)
        return True
    except PairingError as exc:
        logger.error("hermes.config_sync.endpoint_unsafe", extra={"reason": str(exc)})
        return False


def _validate_pubkey(pubkey_hex: str) -> bool:
    """P2: pubkey_hex must be exactly 64 valid hex chars (32-byte Ed25519 key)."""
    if not pubkey_hex or len(pubkey_hex) != _PUBKEY_HEX_LEN:
        logger.error(
            "hermes.config_sync.pubkey_invalid",
            extra={"length": len(pubkey_hex) if pubkey_hex else 0},
        )
        return False
    try:
        bytes.fromhex(pubkey_hex)
        return True
    except ValueError:
        logger.error("hermes.config_sync.pubkey_not_hex")
        return False


def _fetch_bundle(*, url: str, instance_secret: str) -> PolicyBundle | None:
    """HTTP GET the policy bundle with body-size cap.  Returns None on any error."""
    try:
        # Fase 7 injection point: add cert=(<client_cert_path>, <key_path>)
        # to enable mTLS once the cloud issues client certs at pairing time.
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {instance_secret}"},
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,  # SSRF mitigation
        )
    except httpx.HTTPError as exc:
        logger.warning("hermes.config_sync.fetch_error", extra={"reason": str(exc)})
        return None

    if resp.status_code == 204:
        logger.debug("hermes.config_sync.no_new_policy")
        return None

    if resp.status_code != 200:
        logger.warning("hermes.config_sync.fetch_http_error", extra={"status": resp.status_code})
        return None

    # P1-3: enforce body size cap before parsing.
    content = resp.content
    if len(content) > _MAX_BODY_BYTES:
        logger.warning(
            "hermes.config_sync.body_too_large",
            extra={"size": len(content), "limit": _MAX_BODY_BYTES},
        )
        return None

    try:
        import json  # noqa: PLC0415
        data = json.loads(content)
        return PolicyBundle.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.config_sync.parse_error", extra={"reason": str(exc)[:200]})
        return None


def _check_freshness(issued_at: str) -> bool:
    """P1-1: Reject bundles outside the acceptance window.

    issued_at must be ISO-8601 UTC (trailing Z or +00:00).  Bundles that are:
    - more than _FRESHNESS_FUTURE_S seconds in the future (clock skew or replay)
    - more than _FRESHNESS_PAST_S seconds in the past (stale bundle)
    are rejected.
    """
    try:
        # Normalise: replace trailing Z with +00:00 for fromisoformat (py3.10 compat).
        normalised = issued_at.replace("Z", "+00:00")
        ts = datetime.fromisoformat(normalised)
        if ts.tzinfo is None:
            logger.warning("hermes.config_sync.issued_at_no_tz", extra={"issued_at": issued_at})
            return False
        now = datetime.now(tz=UTC)
        age = (now - ts).total_seconds()
        skew = (ts - now).total_seconds()
        if skew > _FRESHNESS_FUTURE_S:
            logger.warning(
                "hermes.config_sync.bundle_too_far_future",
                extra={"issued_at": issued_at, "skew_s": skew},
            )
            return False
        if age > _FRESHNESS_PAST_S:
            logger.warning(
                "hermes.config_sync.bundle_too_old",
                extra={"issued_at": issued_at, "age_s": age},
            )
            return False
        return True
    except (ValueError, TypeError) as exc:
        logger.warning(
            "hermes.config_sync.issued_at_parse_error",
            extra={"issued_at": issued_at, "reason": str(exc)},
        )
        return False


async def _sync_once(
    *,
    store: SQLiteAssociationStore,
    proxy: DbusRuntimeProxy,
) -> None:
    """One sync tick — verify-first ordering (P0-2)."""
    assoc = store.get()
    if assoc is None:
        return

    if not _endpoint_is_safe(assoc.cloud_endpoint):
        return

    # P2: validate pubkey before any network call so we fail loudly on misconfiguration.
    if not _validate_pubkey(assoc.signing_pubkey_hex):
        return

    instance_secret = store.reveal_instance_secret()
    if not instance_secret:
        logger.warning("hermes.config_sync.no_instance_secret")
        return

    url = _policy_url(assoc.cloud_endpoint, assoc.instance_id, assoc.last_applied_version)
    bundle = _fetch_bundle(url=url, instance_secret=instance_secret)
    if bundle is None:
        return

    # P0-2: verify signature FIRST — before checking tenant_id or version.
    # This ensures all subsequent checks are over cryptographically authenticated data.
    envelope = signing_bytes(
        version=bundle.version,
        tenant_id=bundle.tenant_id,
        issued_at=bundle.issued_at,
        payload=bundle.payload,
    )
    if not verify_bundle(
        payload_canonical=envelope,
        signature_hex=bundle.signature_hex,
        pubkey_hex=assoc.signing_pubkey_hex,
    ):
        logger.warning(
            "hermes.config_sync.signature_rejected",
            extra={"version": bundle.version},
        )
        return

    # Only after valid signature: check tenant_id (now over a signed value).
    if bundle.tenant_id != assoc.tenant_id:
        logger.warning(
            "hermes.config_sync.tenant_mismatch",
            extra={"expected": assoc.tenant_id, "received": bundle.tenant_id},
        )
        return

    # P1-1: freshness gate (after signature to avoid timing side-channel).
    if not _check_freshness(bundle.issued_at):
        return

    # Anti-rollback gate.
    if bundle.version <= assoc.last_applied_version:
        logger.debug(
            "hermes.config_sync.version_skip",
            extra={"bundle_version": bundle.version, "last_applied": assoc.last_applied_version},
        )
        return

    applier = PolicyApplier(proxy)
    apply_result = await applier.apply(bundle.payload, tenant_id=bundle.tenant_id)

    if apply_result.ok:
        store.set_last_applied_version(bundle.version)
        store.update_license(bundle.payload.license.model_dump())
        logger.info(
            "hermes.config_sync.applied",
            extra={"version": bundle.version, "applied": apply_result.applied},
        )
    else:
        logger.warning(
            "hermes.config_sync.partial_failure",
            extra={
                "version": bundle.version,
                "failed": apply_result.failed,
                "applied": apply_result.applied,
            },
        )


async def _run_loop() -> None:
    store = _build_store()

    if not store.is_associated():
        logger.info("hermes.config_sync.not_associated — community edition; exiting sync loop")
        return

    proxy = DbusRuntimeProxy()
    uploader = _build_uploader(store)
    interval = _interval_s()
    logger.info("hermes.config_sync.starting", extra={"interval_s": interval})

    while True:
        # Policy pull — fail-soft.
        try:
            await _sync_once(store=store, proxy=proxy)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.config_sync.unhandled_error",
                extra={"reason": str(exc)},
                exc_info=True,
            )

        # Usage upload — independent; a pull failure does not suppress upload.
        try:
            uploader.upload_once()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.config_sync.uploader.unhandled_error",
                extra={"reason": str(exc)},
                exc_info=True,
            )

        # Enterprise remote-approval push/poll (Fase 2 Phase 4b) — independent;
        # already fail-soft internally (run_remote_approvals_once never raises),
        # this try/except is defense-in-depth so a bug there can never take down
        # the policy-pull/usage-upload tick.
        try:
            from hermes.config_sync.remote_approvals import (  # noqa: PLC0415
                run_remote_approvals_once,
            )

            run_remote_approvals_once(store=store)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.config_sync.remote_approvals.unhandled_error",
                extra={"reason": str(exc)},
                exc_info=True,
            )

        # FASE 3 (A2A cross-human) — delegation inbox push/poll: independent;
        # already fail-soft internally (run_delegation_inbox_once never
        # raises), this try/except is defense-in-depth so a bug there can
        # never take down the policy-pull/usage-upload/remote-approvals tick.
        try:
            from hermes.config_sync.delegation_inbox import (  # noqa: PLC0415
                run_delegation_inbox_once,
            )

            await run_delegation_inbox_once(store=store, proxy=proxy)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.config_sync.delegation_inbox.unhandled_error",
                extra={"reason": str(exc)},
                exc_info=True,
            )

        jitter = random.uniform(0, _MAX_JITTER_S)
        await asyncio.sleep(interval + jitter)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    asyncio.run(_run_loop())


if __name__ == "__main__":
    main()
