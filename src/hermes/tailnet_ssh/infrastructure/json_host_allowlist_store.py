"""JsonHostAllowlistStore — the owner's per-host SSH grant store.

Persists `/var/lib/hermes/tailscale/ssh-allowlist.json` as
`{"governed": bool, "hosts": {"<host>": {"approved_at": ..., "managed_by":
..., "identity": ..., "capabilities": [...], "superseded_local_approved_at":
...}}}` — mirrors the `{"domains": [...]}` shape `shell_server/egress_api.py`
uses for the owner's egress grants in spirit (own file, own bounded
context: this store is tailnet_ssh's alone, never shared with the egress
plane), extended with a per-host timestamp so the "Equipos con SSH
aprobado" admin screen can show WHEN each host was granted.

`managed_by` (spec 002 US3, D-4): `None` for a host the owner granted
locally (via the per-host HITL card, `security_hook._resolve_tailnet_ssh_
consent` -> `allow()`); `"cloud"` for a host Enterprise's governed-SSH
policy granted through config-sync's `allow_ssh_host` verb -> `allow_
governed()`. `identity`/`capabilities` are the CAPABILITY CEILING (spec 002
US3) and are ONLY ever meaningful for a `managed_by="cloud"` entry — a local
entry's `identity`/`capabilities` are always None (no ceiling: a local
grant is host-only, unrestricted in WHAT it may do or WHICH remote user it
becomes, exactly as before this feature).

FR-014, "lo heredado manda" (security review 2026-09, B2 — corrects the
original inverted reading): a CLOUD grant SHADOWS a pre-existing LOCAL
entry for the SAME host. `allow_governed()` REPLACES a local entry with the
cloud one — the cloud ceiling becomes authoritative immediately — but the
local approval is never destroyed: its `approved_at` survives as the new
record's `superseded_local_approved_at`, an inert marker (US3 criterion 3,
"todo equipo no declarado queda denegado": once an org governs SSH for an
instance, an unrestricted local grant can never keep shadowing the org's
declared ceiling). If that cloud grant is later revoked, `revoke()` RESTORES
the superseded local entry (its original `approved_at`, `managed_by=None`,
no ceiling) rather than leaving the host in limbo — the local approval was
never actually lost, only dormant while the cloud governed it. `allow()`
(the local path) still never touches an EXISTING entry of any origin — it
cannot re-approve, narrow, or widen a cloud-managed host; only
`allow_governed`'s own shadowing path may replace one, and only in the
cloud's favour.

`governed` (top-level flag, security review 2026-09, B2c): set once, to
`True`, the first time `allow_governed()` is ever called for this instance
(a real governed-SSH grant from config-sync) — and NEVER reset back to
`False` afterward, even if the org later stops publishing any `ssh` section
(default-deny stays the safer posture once an org has opted in). While
`governed` is true, `security_hook`'s local HITL approval path is CLOSED
for any host not already on the allow-list: an org that governs SSH gets
"todo equipo no declarado queda denegado" for real, not just for the hosts
it happened to publish so far. An instance that has never received an `ssh`
section keeps today's local-approval-by-HITL-card behaviour unchanged.

Reads the OLDER `{"hosts": [...]}` array shape too (pre-admin-UI format,
all metadata unknown -> None, governed defaults to False) — additive, never
a breaking migration.

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
    legitimate never-used-yet state -> `{}`, `governed=False`) from "file
    exists but is unreadable or corrupt" (-> raises
    `AllowlistStoreUnavailableError`). Read-modify-write methods NEVER
    catch this: they refuse to write, rather than silently treating "can't
    read" as "empty" and clobbering every surviving entry with just the one
    new write. `grant_for` (the capability-ceiling read) NEVER catches it
    either: a ceiling decision that cannot read its data source denies, it
    does not fall open. `is_allowed` (the coarse per-host gate) is the ONE
    reader that DOES catch it and returns False — "not yet allowed" is
    itself the safe, fail-closed answer for that gate. `is_governed` ALSO
    catches it, but fails closed the OTHER way — returns True (assume
    governed, keep the local-approval path CLOSED) when uncertain, since
    "openly approving a brand new local host" is the unsafe direction here.
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
from dataclasses import dataclass, field
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
    superseded_local_approved_at: str | None = None


@dataclass(frozen=True, slots=True)
class _HostRecord:
    approved_at: str | None
    managed_by: str | None
    identity: str | None = None
    capabilities: tuple[str, ...] | None = None
    superseded_local_approved_at: str | None = None


@dataclass(frozen=True, slots=True)
class _StoreState:
    hosts: dict[str, _HostRecord] = field(default_factory=dict)
    governed: bool = False


class JsonHostAllowlistStore:
    def __init__(self, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH) -> None:
        self._path = allowlist_path

    def is_allowed(self, host: str) -> bool:
        with self._locked(fcntl.LOCK_SH):
            try:
                state = self._load()
            except AllowlistStoreUnavailableError as exc:
                logger.warning(
                    "hermes.tailnet_ssh.allowlist_unavailable operation=is_allowed error=%s",
                    exc,
                )
                return False  # fail-closed: corruption never grants access
        return host.lower() in state.hosts

    def is_governed(self) -> bool:
        """True once this instance has EVER received a real governed-SSH
        grant from config-sync (`allow_governed` sets this permanently).
        Fails closed to True (assume governed, keep local approval CLOSED)
        when the store cannot be read — see module docstring."""
        with self._locked(fcntl.LOCK_SH):
            try:
                state = self._load()
            except AllowlistStoreUnavailableError as exc:
                logger.warning(
                    "hermes.tailnet_ssh.allowlist_unavailable operation=is_governed error=%s",
                    exc,
                )
                return True
        return state.governed

    def allow(self, host: str) -> None:
        """LOCAL owner-approval grant. Idempotent: granting an already-
        allowed host is a no-op — a pre-existing entry (local OR cloud) is
        NEVER overwritten, so this can never change who owns, narrow, or
        widen the ceiling of, an existing entry; a cloud-managed host can
        never be silently "re-approved" back to unrestricted this way.
        Raises `AllowlistStoreUnavailableError` (refuses to write) rather
        than rewriting the file from an assumed-empty state when the
        CURRENT file is corrupt."""
        with self._locked(fcntl.LOCK_EX):
            state = self._load()
            normalized = host.lower()
            if normalized in state.hosts:
                return
            state.hosts[normalized] = _HostRecord(
                approved_at=datetime.now(tz=UTC).isoformat(), managed_by=None
            )
            self._save(state.hosts, governed=state.governed)

    def allow_governed(
        self, host: str, *, identity: str, capabilities: tuple[str, ...]
    ) -> bool:
        """CLOUD-managed grant (spec 002 US3, D-4): create, or UPDATE the
        ceiling of an existing cloud-managed entry for `host`. SHADOWS a
        pre-existing LOCAL entry for the same host (FR-014, "lo heredado
        manda") — the cloud ceiling becomes authoritative immediately; the
        local approval is preserved as an inert `superseded_local_approved_
        at` marker, never destroyed (see module docstring). Marks this
        instance `governed=True`, permanently. Raises
        `AllowlistStoreUnavailableError` (refuses to write) rather than
        rewriting the file from an assumed-empty state when the CURRENT
        file is corrupt.

        Returns True when this call shadowed a pre-existing LOCAL entry
        (the caller reports this as a conflict — informational, the grant
        WAS applied) — False otherwise (a fresh host, or an update to an
        already cloud-managed one).
        """
        with self._locked(fcntl.LOCK_EX):
            state = self._load()
            normalized = host.lower()
            existing = state.hosts.get(normalized)
            shadowed_local = existing is not None and existing.managed_by != _CLOUD_MANAGED
            if existing is not None and existing.managed_by == _CLOUD_MANAGED:
                approved_at = existing.approved_at
                superseded_local_approved_at = existing.superseded_local_approved_at
            elif existing is not None:
                approved_at = datetime.now(tz=UTC).isoformat()
                superseded_local_approved_at = existing.approved_at
            else:
                approved_at = datetime.now(tz=UTC).isoformat()
                superseded_local_approved_at = None
            state.hosts[normalized] = _HostRecord(
                approved_at=approved_at,
                managed_by=_CLOUD_MANAGED,
                identity=identity,
                capabilities=_canonical_capabilities(capabilities),
                superseded_local_approved_at=superseded_local_approved_at,
            )
            self._save(state.hosts, governed=True)
            return shadowed_local

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
            record = self._load().hosts.get(host.lower())
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
            return frozenset(self._load().hosts)

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
            hosts = self._load().hosts
        return [
            AllowedHost(
                host=h,
                approved_at=r.approved_at,
                managed_by=r.managed_by,
                identity=r.identity,
                capabilities=r.capabilities,
                superseded_local_approved_at=r.superseded_local_approved_at,
            )
            for h, r in sorted(hosts.items())
        ]

    def revoke(self, host: str) -> bool:
        """Raises `AllowlistStoreUnavailableError` (refuses to write) rather
        than rewriting the file from an assumed-empty state when the
        CURRENT file is corrupt — a blind revoke-by-rewrite would discard
        every other surviving entry.

        If the entry being revoked was a cloud grant that had SHADOWED a
        local approval, the local entry is RESTORED (its original
        `approved_at`, `managed_by=None`, no ceiling) rather than leaving
        the host unmanaged — the local approval was dormant, not deleted.

        Returns True when a shadowed local entry was restored this way.
        """
        with self._locked(fcntl.LOCK_EX):
            state = self._load()
            normalized = host.lower()
            removed = state.hosts.pop(normalized, None)
            restored_local = False
            if (
                removed is not None
                and removed.managed_by == _CLOUD_MANAGED
                and removed.superseded_local_approved_at is not None
            ):
                state.hosts[normalized] = _HostRecord(
                    approved_at=removed.superseded_local_approved_at, managed_by=None
                )
                restored_local = True
            self._save(state.hosts, governed=state.governed)
            return restored_local

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

    def _load(self) -> _StoreState:
        """Raises `AllowlistStoreUnavailableError` for anything other than
        "file does not exist yet" — an empty store and a CORRUPT store must
        never be indistinguishable to a caller (CWE-636)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _StoreState()
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
        return _StoreState(
            hosts=_normalize_hosts(data.get("hosts", [])),
            governed=bool(data.get("governed", False)),
        )

    def _save(self, hosts: dict[str, _HostRecord], *, governed: bool) -> None:
        """Atomic write: temp file in the SAME directory, fsync, os.replace,
        then fsync the parent directory — the rename itself survives a
        crash, and a reader skipping the lock never observes a partial
        file. Caller MUST already hold LOCK_EX (see the public methods)."""
        self._ensure_dir()
        payload = {
            "governed": governed,
            "hosts": {
                h: {
                    "approved_at": r.approved_at,
                    "managed_by": r.managed_by,
                    "identity": r.identity,
                    "capabilities": list(r.capabilities) if r.capabilities else None,
                    "superseded_local_approved_at": r.superseded_local_approved_at,
                }
                for h, r in sorted(hosts.items())
            },
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
            superseded = (
                meta.get("superseded_local_approved_at") if isinstance(meta, dict) else None
            )
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
                superseded_local_approved_at=superseded if isinstance(superseded, str) else None,
            )
        return result
    if isinstance(raw, list):
        return {
            str(h).lower(): _HostRecord(approved_at=None, managed_by=None)
            for h in raw
            if h
        }
    return {}
