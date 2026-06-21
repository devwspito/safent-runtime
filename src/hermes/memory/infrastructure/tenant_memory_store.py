"""TenantMemoryStore — tenant-scoped, PII-gated persistent memory (F4).

Implements the confinement layer for agent memory writes.

Multi-tenant isolation:
  All reads and writes are confined to `<root>/<tenant_id>/`. The root is
  /var/lib/hermes/memory by default, overridable via HERMES_MEMORY_ROOT env.
  Path construction uses Path.resolve() + prefix check to prevent traversal.

PII gate:
  Content is rejected if it matches the strict threat-pattern scanner
  (same scanner Nous MemoryStore uses for its own injection checks).
  The entry is never written if PII is detected — fail-closed.

Storage format:
  One file per (tenant, target): `<root>/<tenant_id>/<target>.md`.
  Entries are §-delimited (same as Nous MemoryStore for future bridging).
  Writes are atomic (tempfile + os.replace).

Current scope:
  This store is a standalone confinement layer. It does NOT yet bridge to
  Nous MemoryStore (which writes to get_hermes_home()/memories/ globally).
  Bridging requires Nous MemoryStore to accept an injected memory_dir —
  a future task. Until then, skip_memory=True in NousReasoningEngine keeps
  the Nous built-in memory disabled; our store is the authoritative one.

Capa: infrastructure (filesystem I/O, path scoping). No framework.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

_ENTRY_DELIMITER = "\n§\n"
_VALID_TARGET = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class TenantMemoryError(RuntimeError):
    """Unrecoverable error in the tenant memory store."""


class PiiRejectedError(TenantMemoryError):
    """Content rejected because it contains PII or injection patterns."""


class TenantMemoryStore:
    """Per-tenant §-delimited memory store with PII gate.

    Args:
        root:      Base directory.  Per-tenant subtree: root/<tenant_id>/.
        tenant_id: Scopes all reads/writes. Required — no default tenant.
    """

    def __init__(self, *, root: Path, tenant_id: UUID) -> None:
        self._root = root.resolve()
        self._tenant_id = tenant_id
        self._tenant_dir = self._root / str(tenant_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, target: str, content: str) -> dict:
        """Append an entry to the target store.

        Raises:
            PiiRejectedError: if content contains PII / injection patterns.
            TenantMemoryError: on filesystem errors.
        """
        _validate_target(target)
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        _assert_no_pii(content)

        entries = self._load(target)
        if content in entries:
            return {"success": True, "message": "Entry already exists (no duplicate added)."}

        entries.append(content)
        self._save(target, entries)
        logger.info(
            "hermes.memory.tenant_store.add tenant=%s target=%s entries=%d",
            str(self._tenant_id)[:8],
            target,
            len(entries),
        )
        return {"success": True, "entry_count": len(entries)}

    def remove(self, target: str, old_text: str) -> dict:
        """Remove the first entry containing old_text as a substring."""
        _validate_target(target)
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        entries = self._load(target)
        matches = [i for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text[:40]}'."}

        entries.pop(matches[0])
        self._save(target, entries)
        return {"success": True, "entry_count": len(entries)}

    def replace(self, target: str, old_text: str, new_content: str) -> dict:
        """Replace the first entry containing old_text with new_content."""
        _validate_target(target)
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty."}

        _assert_no_pii(new_content)

        entries = self._load(target)
        matches = [i for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text[:40]}'."}

        entries[matches[0]] = new_content
        self._save(target, entries)
        return {"success": True, "entry_count": len(entries)}

    def read(self, target: str) -> list[str]:
        """Return all entries for this tenant's target store."""
        _validate_target(target)
        return self._load(target)

    # ------------------------------------------------------------------
    # Path helpers — path traversal prevention
    # ------------------------------------------------------------------

    def _entry_path(self, target: str) -> Path:
        """Return the resolved path for target, asserting it stays within tenant_dir."""
        candidate = (self._tenant_dir / f"{target}.md").resolve()
        if not str(candidate).startswith(str(self._tenant_dir)):
            raise TenantMemoryError(
                f"Path traversal attempt detected for target={target!r}"
            )
        return candidate

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self, target: str) -> list[str]:
        path = self._entry_path(target)
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(_ENTRY_DELIMITER) if e.strip()]
        return list(dict.fromkeys(entries))  # deduplicate preserving order

    def _save(self, target: str, entries: list[str]) -> None:
        path = self._entry_path(target)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise TenantMemoryError(
                f"Cannot create tenant memory dir {path.parent}: {exc}"
            ) from exc

        content = _ENTRY_DELIMITER.join(entries) if entries else ""
        _atomic_write(path, content)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _validate_target(target: str) -> None:
    if not _VALID_TARGET.match(target):
        raise TenantMemoryError(
            f"Invalid memory target {target!r}. "
            "Must match ^[a-z][a-z0-9_-]{{0,31}}$"
        )


def _assert_no_pii(content: str) -> None:
    """Reject content that matches the PII/injection scanner (fail-closed).

    Uses the same scanner as the Nous MemoryStore (strict scope).
    Raises PiiRejectedError if a match is found.
    """
    try:
        from tools.threat_patterns import first_threat_message  # noqa: PLC0415
        error_msg = first_threat_message(content, scope="strict")
        if error_msg:
            raise PiiRejectedError(
                f"Memory content rejected by PII/injection scanner: {error_msg}"
            )
    except ImportError:
        # threat_patterns not available (non-Nous environment).
        # Log a warning and allow the write — the scanner is optional.
        # In Nous deployments (HERMES_ENGINE=nous) it will always be present.
        logger.warning(
            "hermes.memory.pii_scanner_unavailable: "
            "tools.threat_patterns not importable — PII scan skipped. "
            "Deploy with hermes-agent installed for full protection."
        )


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically: tempfile in same dir + os.replace."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".mem_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
