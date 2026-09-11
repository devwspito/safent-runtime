"""Durable process generations for existing daemon configuration authority.

This module never resolves a provider or releases a credential. A generation
is evidence of configuration continuity, not permission to run inference.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from hermes.runtime.model_config import ManagedProviderUnavailableError
from hermes.security.configuration_lock import configuration_lock

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_llm_lifecycle (
    id INTEGER PRIMARY KEY CHECK(id=1),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    fingerprint TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('local', 'managed', 'blocked')),
    booted_generation INTEGER,
    restart_pending INTEGER NOT NULL CHECK(restart_pending IN (0, 1))
)
"""


class LifecycleUnavailable(ManagedProviderUnavailableError):
    """The current process cannot admit work under its boot authority."""


@dataclass(frozen=True)
class Generation:
    generation: int
    fingerprint: str
    mode: str
    restart_pending: bool


def association_fingerprint(instance_id, tenant_id, signing_key, cloud_endpoint) -> str:
    """Bind a verified policy to the association that supplied its trust key."""
    return hashlib.sha256(
        json.dumps(
            [instance_id, tenant_id, signing_key, cloud_endpoint], separators=(",", ":")
        ).encode()
    ).hexdigest()


def _authority(conn: sqlite3.Connection) -> tuple[str, str]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    policy_row = (
        conn.execute("SELECT state_json FROM managed_llm_policy WHERE id=1").fetchone()
        if "managed_llm_policy" in tables
        else None
    )
    if policy_row is None:
        # Unpair can deliberately restore a LOCAL process on its NEXT boot.
        # The old process sees this changed fingerprint and remains latched shut.
        return "local", hashlib.sha256(b"local:no-managed-policy").hexdigest()
    association = (
        conn.execute(
            "SELECT instance_id,tenant_id,state,signing_pubkey_hex,cloud_endpoint "
            "FROM instance_association WHERE id=1"
        ).fetchone()
        if "instance_association" in tables
        else None
    )
    try:
        policy = json.loads(policy_row[0])
        active = (
            isinstance(policy, dict)
            and policy.get("status") == "active"
            and bool(policy.get("digest"))
            and bool(policy.get("bindings"))
            and association is not None
            and tuple(association[:3])
            == (policy.get("instance_id"), policy.get("tenant_id"), "active")
            and policy.get("association_fingerprint")
            == association_fingerprint(
                association[0], association[1], association[3], association[4]
            )
        )
    except (ValueError, TypeError, RecursionError):
        active = False
    # Hash, never persist raw policy/association: generation metadata has no
    # keys, URLs, user text or provider credentials, including malformed input.
    encoded = json.dumps(
        [policy_row[0], list(association) if association is not None else None],
        separators=(",", ":"),
    ).encode()
    return "managed" if active else "blocked", hashlib.sha256(encoded).hexdigest()


def record_authority_transition(conn: sqlite3.Connection) -> Generation:
    """Reconcile inside the caller's authority-write transaction and config lock."""
    conn.execute(_SCHEMA)
    mode, fingerprint = _authority(conn)
    row = conn.execute(
        "SELECT generation,fingerprint,mode,restart_pending FROM managed_llm_lifecycle WHERE id=1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO managed_llm_lifecycle VALUES(1,1,?,?,NULL,1)", (fingerprint, mode)
        )
        return Generation(1, fingerprint, mode, True)
    if row[1] != fingerprint or row[2] != mode:
        generation = row[0] + 1
        conn.execute(
            "UPDATE managed_llm_lifecycle SET generation=?,fingerprint=?,mode=?,restart_pending=1 "
            "WHERE id=1",
            (generation, fingerprint, mode),
        )
        return Generation(generation, fingerprint, mode, True)
    return Generation(row[0], row[1], row[2], bool(row[3]))


def reconcile_generation(db_path: Path) -> Generation:
    """Recover a pending transition even if the configuration writer crashed."""
    try:
        with configuration_lock(db_path), sqlite3.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return record_authority_transition(conn)
    except (sqlite3.Error, ValueError, TypeError, OSError):
        raise LifecycleUnavailable("Runtime configuration generation unavailable") from None


def acknowledge_boot(db_path: Path, expected: Generation) -> Generation:
    """Record completed bootstrap with CAS; this is not an inference grant.

    Call only after clean-process/profile verification. Admission separately
    checks current authority, and the managed execution gate remains in force.
    """
    try:
        with configuration_lock(db_path), sqlite3.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = record_authority_transition(conn)
            if (current.generation, current.fingerprint, current.mode) != (
                expected.generation,
                expected.fingerprint,
                expected.mode,
            ):
                raise LifecycleUnavailable("Runtime configuration changed during bootstrap")
            conn.execute(
                "UPDATE managed_llm_lifecycle SET booted_generation=?,restart_pending=0 WHERE id=1",
                (current.generation,),
            )
            return Generation(current.generation, current.fingerprint, current.mode, False)
    except (sqlite3.Error, ValueError, TypeError, OSError):
        raise LifecycleUnavailable("Runtime configuration bootstrap unavailable") from None


class ProcessAdmission:
    """A process cannot resume after authority changes, even if it changes back."""

    def __init__(self, db_path: Path, boot: Generation):
        self.db_path = db_path
        self.boot = boot
        self.pid = os.getpid()
        self._closed = False
        self._gate = threading.Lock()
        self._interrupts = set()

    def close(self) -> None:
        """Logical closure only: never invoke native callbacks on an admission path."""
        with self._gate:
            self._closed = True

    def interrupt_inflight(self) -> None:
        """Shutdown phase only, AFTER the independent hard deadline is armed."""
        with self._gate:
            callbacks = tuple(self._interrupts)
            self._interrupts.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Shutdown's independent hard deadline remains authoritative.
                logger.warning("hermes.runtime.native_interrupt_failed")

    def register_interrupt(self, callback):
        with self._gate:
            if self._closed:
                raise LifecycleUnavailable("Runtime restart required before admitting work")
            self._interrupts.add(callback)

        def release():
            with self._gate:
                self._interrupts.discard(callback)

        return release

    def check(self, *, allow_blocked: bool = False) -> Generation:
        with self._gate:
            if self._closed or os.getpid() != self.pid:
                raise LifecycleUnavailable("Runtime restart required before admitting work")
        # Never hold the in-process latch while waiting on a DB/file lock.
        # A caller holding configuration_lock may close admission at any time.
        try:
            current = reconcile_generation(self.db_path)
        except Exception:
            self.close()
            raise
        with self._gate:
            if (
                self._closed
                or os.getpid() != self.pid
                or current.generation != self.boot.generation
                or current.fingerprint != self.boot.fingerprint
                or current.mode != self.boot.mode
                or current.restart_pending
            ):
                self._closed = True
                raise LifecycleUnavailable("Runtime restart required before admitting work")
            if current.mode == "blocked" and not allow_blocked:
                # A freshly booted revoked process stays idle and manageable;
                # denying work must not produce an endless restart loop.
                raise LifecycleUnavailable("Enterprise inference is unavailable")
            return current


async def watch_authority(admission: ProcessAdmission, request_shutdown, *, interval: float = 0.5):
    """Existing daemon idle watcher; never executes work or picks a provider."""
    while True:
        try:
            await asyncio.to_thread(admission.check, allow_blocked=True)
        except LifecycleUnavailable:
            request_shutdown()
            return
        await asyncio.sleep(interval)


async def run_admitted_native(admission: ProcessAdmission | None, call, interrupt):
    """Retain interruption until the actual worker exits, not its cancelled await.

    Uses the daemon's existing default executor. Cancellation before the worker
    starts prevents native invocation; cancellation after it starts must not
    unregister the still-running native request from subsequent shutdown.
    """
    cancelled = threading.Event()

    def worker():
        if cancelled.is_set():
            return None
        release = None
        try:
            if admission is not None:
                admission.check()
                release = admission.register_interrupt(interrupt)
            if cancelled.is_set():
                return None
            return call()
        finally:
            if release is not None:
                release()

    try:
        return await asyncio.get_running_loop().run_in_executor(None, worker)
    except asyncio.CancelledError:
        cancelled.set()
        raise
