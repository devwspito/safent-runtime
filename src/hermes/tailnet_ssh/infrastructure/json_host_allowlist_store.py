"""JsonHostAllowlistStore — the owner's per-host SSH grant store.

Persists `/var/lib/hermes/tailscale/ssh-allowlist.json` as
`{"hosts": {"<host>": {"approved_at": "<iso8601 UTC>", "managed_by": ...}}}`
— mirrors the `{"domains": [...]}` shape `shell_server/egress_api.py` uses
for the owner's egress grants in spirit (own file, own bounded context: this
store is tailnet_ssh's alone, never shared with the egress plane), extended
with a per-host timestamp so the "Equipos con SSH aprobado" admin screen can
show WHEN each host was granted.

`managed_by` (spec 002 US3, D-4): `None` for a host the owner granted
locally (via the `/api/v1/tailnet/ssh-hosts` admin screen); `"cloud"` for a
host Enterprise's governed-SSH policy granted through config-sync's
`allow_ssh_host` verb. `allow()` is idempotent REGARDLESS of origin — a
pre-existing entry (local OR cloud) is NEVER overwritten by a later `allow()`
call (FR-014: a local grant is never silently replaced) — callers that need
to detect "this host is already locally-owned" read `managed_by` off
`list_with_metadata()` BEFORE calling `allow()`.

Reads the OLDER `{"hosts": [...]}` array shape too (pre-admin-UI format,
approved_at and managed_by reported as `None`) — additive, never a breaking
migration.

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

DEFAULT_ALLOWLIST_PATH = Path("/var/lib/hermes/tailscale/ssh-allowlist.json")


@dataclass(frozen=True, slots=True)
class AllowedHost:
    host: str
    approved_at: str | None
    managed_by: str | None = None


@dataclass(frozen=True, slots=True)
class _HostRecord:
    approved_at: str | None
    managed_by: str | None


class JsonHostAllowlistStore:
    def __init__(self, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH) -> None:
        self._path = allowlist_path

    def is_allowed(self, host: str) -> bool:
        return host.lower() in self._load()

    def allow(self, host: str, *, managed_by: str | None = None) -> None:
        """Idempotent: granting an already-allowed host is a no-op — a
        pre-existing grant (local OR cloud) is NEVER overwritten, so a
        second `allow()` can never change who owns an existing entry."""
        hosts = self._load()
        normalized = host.lower()
        if normalized in hosts:
            return
        hosts[normalized] = _HostRecord(
            approved_at=datetime.now(tz=UTC).isoformat(), managed_by=managed_by
        )
        self._save(hosts)

    def list_allowed(self) -> frozenset[str]:
        return frozenset(self._load())

    def list_with_metadata(self) -> list[AllowedHost]:
        """Approved hosts + when each was granted + who manages it, sorted
        by host name."""
        hosts = self._load()
        return [
            AllowedHost(host=h, approved_at=r.approved_at, managed_by=r.managed_by)
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
                h: {"approved_at": r.approved_at, "managed_by": r.managed_by}
                for h, r in sorted(hosts.items())
            }
        }
        self._path.write_text(json.dumps(payload), encoding="utf-8")


def _normalize_hosts(raw: Any) -> dict[str, _HostRecord]:
    """Accepts both the current `{"host": {"approved_at": ...}}` shape and
    the older bare `[host, ...]` array shape (approved_at/managed_by unknown
    -> None)."""
    if isinstance(raw, dict):
        result: dict[str, _HostRecord] = {}
        for host, meta in raw.items():
            if not host:
                continue
            approved_at = meta.get("approved_at") if isinstance(meta, dict) else None
            managed_by = meta.get("managed_by") if isinstance(meta, dict) else None
            result[str(host).lower()] = _HostRecord(
                approved_at=approved_at, managed_by=managed_by
            )
        return result
    if isinstance(raw, list):
        return {
            str(h).lower(): _HostRecord(approved_at=None, managed_by=None)
            for h in raw
            if h
        }
    return {}
