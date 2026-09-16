"""JsonHostAllowlistStore — the owner's per-host SSH grant store.

Persists `/var/lib/hermes/tailscale/ssh-allowlist.json` as
`{"hosts": {"<host>": {"approved_at": "<iso8601 UTC>"}}}` — mirrors the
`{"domains": [...]}` shape `shell_server/egress_api.py` uses for the owner's
egress grants in spirit (own file, own bounded context: this store is
tailnet_ssh's alone, never shared with the egress plane), extended with a
per-host timestamp so the "Equipos con SSH aprobado" admin screen can show
WHEN each host was granted.

Reads the OLDER `{"hosts": [...]}` array shape too (pre-admin-UI format,
approved_at reported as `None`) — additive, never a breaking migration.

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


class JsonHostAllowlistStore:
    def __init__(self, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH) -> None:
        self._path = allowlist_path

    def is_allowed(self, host: str) -> bool:
        return host.lower() in self._load()

    def allow(self, host: str) -> None:
        hosts = self._load()
        normalized = host.lower()
        if normalized in hosts:
            return
        hosts[normalized] = datetime.now(tz=UTC).isoformat()
        self._save(hosts)

    def list_allowed(self) -> frozenset[str]:
        return frozenset(self._load())

    def list_with_metadata(self) -> list[AllowedHost]:
        """Approved hosts + when each was granted, sorted by host name."""
        hosts = self._load()
        return [AllowedHost(host=h, approved_at=at) for h, at in sorted(hosts.items())]

    def revoke(self, host: str) -> None:
        hosts = self._load()
        hosts.pop(host.lower(), None)
        self._save(hosts)

    def _load(self) -> dict[str, str | None]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return _normalize_hosts(data.get("hosts", []))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, hosts: dict[str, str | None]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "hosts": {h: {"approved_at": at} for h, at in sorted(hosts.items())}
        }
        self._path.write_text(json.dumps(payload), encoding="utf-8")


def _normalize_hosts(raw: Any) -> dict[str, str | None]:
    """Accepts both the current `{"host": {"approved_at": ...}}` shape and
    the older bare `[host, ...]` array shape (approved_at unknown -> None)."""
    if isinstance(raw, dict):
        result: dict[str, str | None] = {}
        for host, meta in raw.items():
            if not host:
                continue
            approved_at = meta.get("approved_at") if isinstance(meta, dict) else None
            result[str(host).lower()] = approved_at
        return result
    if isinstance(raw, list):
        return {str(h).lower(): None for h in raw if h}
    return {}
