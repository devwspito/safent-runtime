"""Verified Enterprise routing policy. Credentials never enter global Hermes env.

This is a selection/authorization layer, not an inference engine: every call
still uses Hermes's native resolver and GovernedAIAgent.
"""

from __future__ import annotations

# Config-sync and runtime modules import each other during daemon bootstrap.
# Keep their existing lazy imports at the verified call boundary.
# ruff: noqa: PLC0415
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from hermes.runtime.model_config import (
    MANAGED_EXECUTION_UNAVAILABLE,
    ManagedProviderUnavailableError,
    ModelConfig,
)
from hermes.security.configuration_lock import configuration_lock

_TABLE = "managed_llm_policy"
_MAX_ENVELOPE_BYTES = 2_000_000
_GATEWAY_SUFFIX_PARTS = 2


@contextmanager
def local_configuration_write(db_path: Path):
    """Serialize a local credential commit with signed management changes.

    OAuth can start while local and finish after management is applied. Check
    at commit time, not only when the login was initiated. Never hold this
    lock during HTTP polling or other network operations.
    """
    with configuration_lock(db_path):
        if read_policy(db_path) is not None:
            raise PermissionError("LLM configuration is managed by Enterprise")
        yield


def read_policy(db_path: Path) -> dict | None:
    return _read_policy(db_path, check_trust=True)


def _read_policy(db_path: Path, *, check_trust: bool) -> dict | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
            ).fetchone()
            if not exists:
                return None
            row = conn.execute("SELECT state_json FROM managed_llm_policy WHERE id=1").fetchone()
            if not row:
                return None
            state = json.loads(row[0])
            association = conn.execute(
                "SELECT instance_id, tenant_id, state, signing_pubkey_hex, cloud_endpoint "
                "FROM instance_association WHERE id=1"
            ).fetchone()
            if not association or association[:3] != (
                state["instance_id"],
                state["tenant_id"],
                "active",
            ):
                raise ManagedProviderUnavailableError("Enterprise association is inactive")
            from hermes.runtime.managed_llm_lifecycle import association_fingerprint

            if check_trust and state.get("association_fingerprint") != association_fingerprint(
                association[0], association[1], association[3], association[4]
            ):
                raise ManagedProviderUnavailableError("Enterprise LLM trust binding changed")
            return state
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise ManagedProviderUnavailableError("LLM policy cannot be verified") from None


def _save_policy(db_path: Path, state: dict) -> None:
    from hermes.runtime.managed_llm_lifecycle import record_authority_transition

    with configuration_lock(db_path), sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS managed_llm_policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), state_json TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO managed_llm_policy VALUES (1, ?)", (json.dumps(state),)
        )
        record_authority_transition(conn)


def resolve_managed_config(db_path: Path, alias: str | None = None) -> ModelConfig | None:  # noqa: ARG001 - execution remains gated
    """Execution boundary: never release a managed token until isolation exists.

    A signed assignment can be stored/revoked and inspected without enabling
    inference. No engine, auxiliary, skill synthesis or MCP caller can obtain
    the delegated credential through the production config source meanwhile.
    """
    from hermes.runtime.managed_llm_bootstrap import assert_process_admission

    # Check even when policy disappeared: unpair must not release an OLD
    # corporate process into a cached personal credential environment.
    assert_process_admission(db_path)
    state = read_policy(db_path)
    if state is None:
        return None
    raise ManagedProviderUnavailableError(MANAGED_EXECUTION_UNAVAILABLE)


def _resolve_managed_binding(db_path: Path, alias: str | None = None) -> ModelConfig | None:
    """Private contract/transport diagnostic; not an execution config source."""
    with configuration_lock(db_path):
        state = read_policy(db_path)
        if state is None:
            return None
        if state["status"] != "active":
            raise ManagedProviderUnavailableError("Enterprise LLM is unavailable or revoked")
        selected = alias or state["active_alias"]
        provider_id = state["bindings"].get(selected)
        if not provider_id:
            raise ManagedProviderUnavailableError("Provider is not assigned by Enterprise")
        from uuid import UUID

        from hermes.shell_server.providers.repo import SQLiteProviderRepository
        from hermes.shell_server.security.secrets import SecretsVault

        try:
            repo = SQLiteProviderRepository(db_path=db_path, vault=SecretsVault())
            provider = repo.get(provider_id=UUID(provider_id))
            key = repo.reveal_api_key(provider_id=UUID(provider_id))
            if (
                not provider.enabled
                or provider.managed_by != "cloud"
                or not key
                or not provider.base_url
            ):
                raise ValueError("Managed provider unavailable")
        except Exception as exc:
            raise ManagedProviderUnavailableError("Enterprise LLM credential unavailable") from exc
        return ModelConfig(
            model=f"custom/{provider.default_model}",
            api_key=key,
            base_url=provider.base_url,
            native_provider="custom",
            managed=True,
        )


def apply_signed_gateway(wiring, bundle_json: str) -> dict:
    """Daemon boundary: accepts only the full signed, instance-bound envelope."""
    store = wiring._association_store
    if store is None:
        raise PermissionError("Enterprise association unavailable")
    repo = wiring._provider_repo
    if repo is None or Path(repo._db_path).resolve() != Path(store.db_path).resolve():
        raise PermissionError("Enterprise configuration stores do not share one scope")
    # Snapshot, signature, current key, freshness and all writes share the same
    # cross-process boundary as unpair/revoke/pair and local OAuth commits.
    with configuration_lock(store.db_path):
        return _apply_signed_gateway_locked(wiring, bundle_json, store)


def _apply_signed_gateway_locked(wiring, bundle_json: str, store) -> dict:  # noqa: PLR0912, PLR0915 - existing signed validation remains contiguous
    from hermes.config_sync.__main__ import _check_freshness
    from hermes.config_sync.applier import _is_safe_base_url
    from hermes.config_sync.policy_document import PolicyBundle, signing_bytes
    from hermes.config_sync.signature import verify_bundle
    from hermes.runtime.managed_llm_lifecycle import association_fingerprint
    from hermes.shell_server.providers.domain import ProviderKind, new_provider

    association = store.get()
    if association is None or association.state != "active":
        raise PermissionError("Enterprise association inactive")
    try:
        if len(bundle_json.encode()) > _MAX_ENVELOPE_BYTES:
            raise ValueError("oversize")
        bundle = PolicyBundle.model_validate_json(bundle_json)
    except ValueError:
        # Pydantic diagnostics include input values; never expose a credential
        # through D-Bus errors or config-sync logging.
        raise PermissionError("Malformed LLM policy envelope") from None
    encoded = signing_bytes(
        version=bundle.version,
        tenant_id=bundle.tenant_id,
        issued_at=bundle.issued_at,
        payload=bundle.payload,
    )
    if not verify_bundle(
        payload_canonical=encoded,
        signature_hex=bundle.signature_hex,
        pubkey_hex=association.signing_pubkey_hex,
    ):
        raise PermissionError("Invalid LLM policy signature")
    if (
        bundle.tenant_id != association.tenant_id
        or bundle.payload.llm_instance_id != association.instance_id
    ):
        raise PermissionError("LLM policy tenant or instance mismatch")
    if not _check_freshness(bundle.issued_at):
        raise PermissionError("LLM policy expired")
    providers = bundle.payload.providers
    if len(providers) > 1:
        raise PermissionError("Community supports at most one managed LLM binding")
    expected_origin = urlparse(association.cloud_endpoint)
    gateway_prefix = expected_origin.path.rstrip("/") + "/v1/inference/"
    for spec in providers:
        endpoint = urlparse(spec.base_url or "")
        suffix = endpoint.path.removeprefix(gateway_prefix).split("/")
        if (
            spec.credential_kind != "instance_gateway"
            or spec.kind != "openai_compatible"
            or not spec.api_key
            or not _is_safe_base_url(spec.base_url or "")
            or (endpoint.scheme, endpoint.netloc)
            != (expected_origin.scheme, expected_origin.netloc)
            or not endpoint.path.startswith(gateway_prefix)
            or len(suffix) != _GATEWAY_SUFFIX_PARTS
            or not suffix[0]
            or suffix[0] in {".", ".."}
            or "%" in suffix[0]
            or suffix[1] != "v1"
            or endpoint.query
            or endpoint.fragment
            or endpoint.username
        ):
            raise PermissionError("Only the associated Enterprise inference gateway is allowed")
    if len({spec.alias for spec in providers}) != len(providers):
        raise PermissionError("Duplicate managed provider aliases")
    active = [spec.alias for spec in providers if spec.set_active]
    if providers and len(active) != 1:
        raise PermissionError("Exactly one managed default provider is required")
    db_path = store.db_path
    digest = hashlib.sha256(encoded).hexdigest()
    trust_binding = association_fingerprint(
        association.instance_id,
        association.tenant_id,
        association.signing_pubkey_hex,
        association.cloud_endpoint,
    )
    with configuration_lock(db_path):
        # The incoming envelope was verified against the CURRENT key above.
        # Preserve the previous replay floor while allowing an authorized fresh
        # policy to replace the old key binding after rotation.
        previous = _read_policy(db_path, check_trust=False)
        if previous:
            if bundle.version < previous["version"]:
                raise PermissionError("LLM policy rollback rejected")
            if bundle.version == previous["version"]:
                if digest != previous["digest"]:
                    raise PermissionError("LLM policy version collision")
                if (
                    previous["status"] != "applying"
                    and previous.get("association_fingerprint") == trust_binding
                ):
                    return {"ok": True, "unchanged": True}
        elif bundle.version < association.last_applied_version:
            raise PermissionError("LLM policy rollback rejected")
        # Publish the blocking state BEFORE touching any credential. A partial
        # failure remains blocked and can retry only the same/newer signed policy.
        state = {
            "version": bundle.version,
            "digest": digest,
            "status": "applying",
            "tenant_id": association.tenant_id,
            "instance_id": association.instance_id,
            "association_fingerprint": trust_binding,
            "active_alias": active[0] if active else None,
            "bindings": {},
        }
        _save_policy(db_path, state)
        repo = wiring._provider_repo
        existing = {provider.alias: provider for provider in repo.list_all()}
        for spec in providers:
            provider = existing.get(spec.alias)
            if provider is not None and provider.managed_by != "cloud":
                raise PermissionError("Managed alias conflicts with a local provider")
            if provider is None:
                provider = new_provider(
                    alias=spec.alias,
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    default_model=spec.default_model,
                    base_url=spec.base_url,
                    has_api_key=True,
                )
                provider.managed_by = "cloud"
                repo.add(provider=provider, api_key=spec.api_key)
            else:
                provider.kind = ProviderKind.OPENAI_COMPATIBLE
                provider.default_model = spec.default_model
                provider.base_url = spec.base_url
                provider.enabled = True
                repo.update(provider=provider, api_key=spec.api_key)
            state["bindings"][spec.alias] = str(provider.provider_id)
        for alias, provider in existing.items():
            if provider.managed_by == "cloud" and alias not in state["bindings"]:
                repo.delete(provider_id=provider.provider_id)
        state["status"] = "active" if providers else "revoked"
        _save_policy(db_path, state)
        if wiring._active_provider_svc is not None:
            wiring._active_provider_svc.force_refresh()
        from hermes.runtime.nous_engine import clear_runtime_provider_cache

        clear_runtime_provider_cache()
        return {"ok": True, "status": state["status"], "version": bundle.version}
