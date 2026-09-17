"""JsonHostAllowlistStore — the owner's per-host SSH grant store.

Persists `/var/lib/hermes/tailscale/ssh-allowlist.json` as
`{"hosts": {"<host>": {"approved_at": ..., "managed_by": ..., "identity":
..., "capabilities": [...]}}}` — mirrors the `{"domains": [...]}` shape
`shell_server/egress_api.py` uses for the owner's egress grants in spirit
(own file, own bounded context: this store is tailnet_ssh's alone, never
shared with the egress plane), extended with a per-host timestamp so the
"Equipos con SSH aprobado" admin screen can show WHEN each host was granted.

`managed_by` (spec 002 US3, D-4): `None` for a host the owner granted
locally (via the per-host HITL card, `security_hook._resolve_tailnet_ssh_
consent` -> `allow()`); `"cloud"` for a host Enterprise's governed-SSH
policy granted through config-sync's `allow_ssh_host` verb -> `allow_
governed()`. `identity`/`capabilities` are the CAPABILITY CEILING (spec 002
US3) and are ONLY ever meaningful for a `managed_by="cloud"` entry — a local
entry's `identity`/`capabilities` are always None (no ceiling: "keep
today's semantics" for whatever the local owner granted — a local grant is
host-only, unrestricted in WHAT it may do or WHICH remote user it becomes,
exactly as before this feature).

Two distinct write paths, two distinct semantics (FR-014):
  - `allow(host)`: the LOCAL owner-approval path. Idempotent — a
    pre-existing entry of ANY origin is NEVER touched.
  - `allow_governed(host, identity=..., capabilities=...)`: the CLOUD path.
    Creates a NEW cloud-managed entry, or UPDATES the ceiling of an
    EXISTING cloud-managed entry (idempotent: the same grant reapplied
    converges to the same state; re-publishing a NARROWER or different
    grant REPLACES the stored ceiling). NEVER touches a pre-existing entry
    whose `managed_by` is NOT already "cloud" — a local grant is never
    silently taken over, enforced HERE (not just by the D-Bus wiring's own
    conflict check) as the authoritative guarantee.

Reads the OLDER `{"hosts": [...]}` array shape too (pre-admin-UI format,
all metadata unknown -> None) — additive, never a breaking migration.

Stateless helpers over one JSON file: cheap enough to construct fresh per
call (no in-memory cache to go stale across daemon restarts or concurrent
callers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes.tailnet_ssh.application.ports import HostGrant

DEFAULT_ALLOWLIST_PATH = Path("/var/lib/hermes/tailscale/ssh-allowlist.json")

_CLOUD_MANAGED = "cloud"

# Mirrors SshHostSpec.capabilities' enum in hermes.config_sync.policy_document
# — the only values a governed-SSH ceiling may ever contain.
_KNOWN_CAPABILITIES = frozenset({"exec", "file_read", "file_write"})


@dataclass(frozen=True, slots=True)
class AllowedHost:
    host: str
    approved_at: str | None
    managed_by: str | None = None
    identity: str | None = None
    capabilities: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class _HostRecord:
    approved_at: str | None
    managed_by: str | None
    identity: str | None = None
    capabilities: tuple[str, ...] | None = None


class JsonHostAllowlistStore:
    def __init__(self, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH) -> None:
        self._path = allowlist_path

    def is_allowed(self, host: str) -> bool:
        return host.lower() in self._load()

    def allow(self, host: str) -> None:
        """LOCAL owner-approval grant. Idempotent: granting an already-
        allowed host is a no-op — a pre-existing entry (local OR cloud) is
        NEVER overwritten, so this can never change who owns, or widen the
        ceiling of, an existing entry."""
        hosts = self._load()
        normalized = host.lower()
        if normalized in hosts:
            return
        hosts[normalized] = _HostRecord(
            approved_at=datetime.now(tz=UTC).isoformat(), managed_by=None
        )
        self._save(hosts)

    def allow_governed(
        self, host: str, *, identity: str, capabilities: tuple[str, ...]
    ) -> None:
        """CLOUD-managed grant (spec 002 US3, D-4): create, or UPDATE the
        ceiling of an existing cloud-managed entry for `host`. A NO-OP when
        `host` already exists with a DIFFERENT `managed_by` (a pre-existing
        LOCAL grant) — FR-014, a local grant is never silently taken over.
        """
        hosts = self._load()
        normalized = host.lower()
        existing = hosts.get(normalized)
        if existing is not None and existing.managed_by != _CLOUD_MANAGED:
            return
        approved_at = existing.approved_at if existing is not None else (
            datetime.now(tz=UTC).isoformat()
        )
        hosts[normalized] = _HostRecord(
            approved_at=approved_at,
            managed_by=_CLOUD_MANAGED,
            identity=identity,
            capabilities=_canonical_capabilities(capabilities),
        )
        self._save(hosts)

    def grant_for(self, host: str) -> HostGrant | None:
        """HostGrantPort implementation — the capability ceiling for `host`.

        None when `host` is not on the allow-list at all. A non-cloud entry
        (local, or malformed/legacy data) ALWAYS reports `capabilities=None`
        (no ceiling) regardless of any stray identity/capabilities value on
        disk — the ceiling only ever applies to `managed_by == "cloud"`.
        """
        record = self._load().get(host.lower())
        if record is None:
            return None
        if record.managed_by != _CLOUD_MANAGED:
            return HostGrant(managed_by=record.managed_by, identity=None, capabilities=None)
        return HostGrant(
            managed_by=record.managed_by,
            identity=record.identity,
            capabilities=frozenset(record.capabilities or ()),
        )

    def list_allowed(self) -> frozenset[str]:
        return frozenset(self._load())

    def list_with_metadata(self) -> list[AllowedHost]:
        """Approved hosts + when each was granted + who manages it (+ the
        capability ceiling, for a cloud-managed entry), sorted by host name.
        """
        hosts = self._load()
        return [
            AllowedHost(
                host=h,
                approved_at=r.approved_at,
                managed_by=r.managed_by,
                identity=r.identity,
                capabilities=r.capabilities,
            )
            for h, r in sorted(hosts.items())
        ]

    def revoke(self, host: str) -> None:
        hosts = self._load()
        hosts.pop(host.lower(), None)
        self._save(hosts)

    def _load(self) -> dict[str, _HostRecord]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return _normalize_hosts(data.get("hosts", []))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, hosts: dict[str, _HostRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "hosts": {
                h: {
                    "approved_at": r.approved_at,
                    "managed_by": r.managed_by,
                    "identity": r.identity,
                    "capabilities": list(r.capabilities) if r.capabilities else None,
                }
                for h, r in sorted(hosts.items())
            }
        }
        self._path.write_text(json.dumps(payload), encoding="utf-8")


def _canonical_capabilities(raw: Any) -> tuple[str, ...]:
    """Fail-closed filter: only the 3 known capability strings survive,
    sorted + deduplicated. Anything else on the wire/disk is silently
    dropped rather than granted — a corrupted or forward-incompatible
    capability name must never widen what a cloud-managed host may do."""
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({c for c in raw if c in _KNOWN_CAPABILITIES}))


def _normalize_hosts(raw: Any) -> dict[str, _HostRecord]:
    """Accepts both the current `{"host": {"approved_at": ...}}` shape and
    the older bare `[host, ...]` array shape (all metadata unknown -> None).
    """
    if isinstance(raw, dict):
        result: dict[str, _HostRecord] = {}
        for host, meta in raw.items():
            if not host:
                continue
            approved_at = meta.get("approved_at") if isinstance(meta, dict) else None
            managed_by = meta.get("managed_by") if isinstance(meta, dict) else None
            identity = meta.get("identity") if isinstance(meta, dict) else None
            capabilities = (
                _canonical_capabilities(meta.get("capabilities"))
                if isinstance(meta, dict) and managed_by == _CLOUD_MANAGED
                else None
            )
            result[str(host).lower()] = _HostRecord(
                approved_at=approved_at,
                managed_by=managed_by,
                identity=identity if isinstance(identity, str) else None,
                capabilities=capabilities or None,
            )
        return result
    if isinstance(raw, list):
        return {
            str(h).lower(): _HostRecord(approved_at=None, managed_by=None)
            for h in raw
            if h
        }
    return {}
