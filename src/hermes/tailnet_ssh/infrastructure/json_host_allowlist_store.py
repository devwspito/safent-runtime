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

CONCURRENCY / CRASH SAFETY (security review 2026-09, B1, CWE-367/636):
  - Every public method holds an `fcntl.flock` on a SEPARATE `.lock`
    sidecar file for its ENTIRE critical section — LOCK_SH for a pure read,
    LOCK_EX spanning BOTH the read AND the write for allow/allow_governed/
    revoke. This serializes concurrent read-modify-write cycles (e.g. a
    local HITL approval racing a config-sync bundle apply) so one never
    clobbers the other's result, and stops a read from observing a writer's
    torn intermediate state.
  - `_save()` writes to a temp file in the SAME directory, fsyncs it,
    `os.replace()`s it over the target (atomic on POSIX — a concurrent
    reader that skips the lock, e.g. an operator's `cat`, still only ever
    sees the fully-old or fully-new file, never a partial one), then fsyncs
    the parent directory so the rename itself survives a crash.
  - `_load()` distinguishes "file does not exist" (empty store, a
    legitimate never-used-yet state -> `{}`) from "file exists but is
    unreadable or corrupt" (-> raises `AllowlistStoreUnavailableError`).
    Read-modify-write methods NEVER catch this: they refuse to write,
    rather than silently treating "can't read" as "empty" and clobbering
    every surviving entry with just the one new write. `grant_for` (the
    capability-ceiling read) NEVER catches it either: a ceiling decision
    that cannot read its data source denies, it does not fall open.
    `is_allowed` (the coarse per-host gate) is the ONE reader that DOES
    catch it and returns False — "not yet allowed" is itself the safe,
    fail-closed answer for that gate (mirrors `security_hook`'s own
    fail-closed posture, and forces a fresh HITL/re-sync rather than
    silently trusting stale state).
  - File mode 0600, parent directory mode 0700 (M1).

Stateless helpers over one JSON file otherwise: cheap enough to construct
fresh per call (no in-memory cache to go stale across daemon restarts).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes.tailnet_ssh.application.ports import HostGrant
from hermes.tailnet_ssh.domain.errors import AllowlistStoreUnavailableError

logger = logging.getLogger("hermes.tailnet_ssh.allowlist_store")

DEFAULT_ALLOWLIST_PATH = Path("/var/lib/hermes/tailscale/ssh-allowlist.json")

_CLOUD_MANAGED = "cloud"
_DIR_MODE = 0o700
_FILE_MODE = 0o600

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
        with self._locked(fcntl.LOCK_SH):
            try:
                hosts = self._load()
            except AllowlistStoreUnavailableError as exc:
                logger.warning(
                    "hermes.tailnet_ssh.allowlist_unavailable operation=is_allowed error=%s",
                    exc,
                )
                return False  # fail-closed: corruption never grants access
        return host.lower() in hosts

    def allow(self, host: str) -> None:
        """LOCAL owner-approval grant. Idempotent: granting an already-
        allowed host is a no-op — a pre-existing entry (local OR cloud) is
        NEVER overwritten, so this can never change who owns, or widen the
        ceiling of, an existing entry. Raises `AllowlistStoreUnavailableError`
        (refuses to write) rather than rewriting the file from an assumed-
        empty state when the CURRENT file is corrupt."""
        with self._locked(fcntl.LOCK_EX):
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
        Raises `AllowlistStoreUnavailableError` (refuses to write) rather
        than rewriting the file from an assumed-empty state when the
        CURRENT file is corrupt.
        """
        with self._locked(fcntl.LOCK_EX):
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

        Raises `AllowlistStoreUnavailableError` when the store cannot be
        read — NEVER caught here, NEVER mapped to `None`: a capability
        decision that cannot read its data source must deny, not silently
        treat the host as unmanaged/unrestricted (CWE-636).
        """
        with self._locked(fcntl.LOCK_SH):
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
        with self._locked(fcntl.LOCK_SH):
            return frozenset(self._load())

    def list_with_metadata(self) -> list[AllowedHost]:
        """Approved hosts + when each was granted + who manages it (+ the
        capability ceiling, for a cloud-managed entry), sorted by host name.
        Raises `AllowlistStoreUnavailableError` when the store is corrupt —
        callers that want a degraded empty listing instead must catch it
        explicitly (this is a read-only admin/introspection path, not an
        access-control decision, so no blanket fail-closed default is
        imposed here).
        """
        with self._locked(fcntl.LOCK_SH):
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
        """Raises `AllowlistStoreUnavailableError` (refuses to write) rather
        than rewriting the file from an assumed-empty state when the
        CURRENT file is corrupt — a blind revoke-by-rewrite would discard
        every other surviving entry."""
        with self._locked(fcntl.LOCK_EX):
            hosts = self._load()
            hosts.pop(host.lower(), None)
            self._save(hosts)

    # ------------------------------------------------------------------
    # Locking, load, save
    # ------------------------------------------------------------------

    @contextmanager
    def _locked(self, lock_mode: int) -> Iterator[None]:
        """Hold an advisory `flock` on a SEPARATE `.lock` sidecar file for
        the caller's entire critical section (LOCK_SH for a read, LOCK_EX
        spanning a read-modify-write). The sidecar is independent of the
        data file so lock acquisition never contends with `_save()`'s own
        atomic replace of the data file.

        A write (LOCK_EX) always ensures the directory exists first — it is
        about to create data there. A read (LOCK_SH) does NOT: creating the
        directory just to take a lock would turn "store never initialised /
        directory not provisioned yet" into a hard error for a plain read,
        which used to fail-soft to an empty store. If the lock file cannot
        even be opened for a read (directory missing, or a genuine
        permissions problem), there is by definition no writer racing
        against a store that was never created — fall through WITHOUT a
        lock and let `_load()` take its normal FileNotFoundError -> `{}`
        path (or raise `AllowlistStoreUnavailableError` if the data file
        itself turns out to exist but be unreadable — same fail-closed
        contract either way)."""
        if lock_mode == fcntl.LOCK_EX:
            self._ensure_dir()
        lock_path = self._path.with_name(self._path.name + ".lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
        except OSError:
            if lock_mode == fcntl.LOCK_EX:
                raise
            yield
            return
        try:
            fcntl.flock(fd, lock_mode)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        # best-effort tightening; mkdir's own mode already applied for a fresh dir
        with suppress(OSError):
            os.chmod(self._path.parent, _DIR_MODE)

    def _load(self) -> dict[str, _HostRecord]:
        """Raises `AllowlistStoreUnavailableError` for anything other than
        "file does not exist yet" — an empty store and a CORRUPT store must
        never be indistinguishable to a caller (CWE-636)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise AllowlistStoreUnavailableError(
                f"no se pudo leer {self._path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AllowlistStoreUnavailableError(
                f"{self._path} contiene JSON corrupto: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise AllowlistStoreUnavailableError(
                f"{self._path}: la forma raíz no es un objeto JSON"
            )
        return _normalize_hosts(data.get("hosts", []))

    def _save(self, hosts: dict[str, _HostRecord]) -> None:
        """Atomic write: temp file in the SAME directory, fsync, os.replace,
        then fsync the parent directory — the rename itself survives a
        crash, and a reader skipping the lock never observes a partial
        file. Caller MUST already hold LOCK_EX (see the public methods)."""
        self._ensure_dir()
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
        raw = json.dumps(payload).encode("utf-8")
        tmp_path = self._path.with_name(f"{self._path.name}.tmp-{uuid.uuid4().hex[:8]}")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            tmp_path.unlink(missing_ok=True)
            raise
        else:
            os.close(fd)
        os.replace(tmp_path, self._path)
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


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
