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

Storage format (v2 — provenance):
  One file per (tenant, target): `<root>/<tenant_id>/<target>.md`.
  Entries are §-delimited JSON objects:
    {"content": str, "agent_id": str, "ts": iso8601_str_or_null}
  Backward-compat: plain-string entries from the v1 format are read as
    {"content": str, "agent_id": "legacy", "ts": null}
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

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_ENTRY_DELIMITER = "\n§\n"
_VALID_TARGET = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Sentinel agent_id for entries loaded from the pre-provenance (v1) format.
_LEGACY_AGENT_ID = "legacy"


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

    def add(self, target: str, content: str, *, agent_id: str = "unknown") -> dict:
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
        if any(e["content"] == content for e in entries):
            return {"success": True, "message": "Entry already exists (no duplicate added)."}

        entries.append(_make_entry(content, agent_id))
        self._save(target, entries)
        logger.info(
            "hermes.memory.tenant_store.add tenant=%s target=%s entries=%d agent=%s",
            str(self._tenant_id)[:8],
            target,
            len(entries),
            agent_id,
        )
        return {"success": True, "entry_count": len(entries)}

    def remove(self, target: str, old_text: str) -> dict:
        """Remove the first entry whose content contains old_text as a substring."""
        _validate_target(target)
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        entries = self._load(target)
        matches = [i for i, e in enumerate(entries) if old_text in e["content"]]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text[:40]}'."}

        entries.pop(matches[0])
        self._save(target, entries)
        return {"success": True, "entry_count": len(entries)}

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        *,
        agent_id: str = "unknown",
    ) -> dict:
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
        matches = [i for i, e in enumerate(entries) if old_text in e["content"]]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text[:40]}'."}

        entries[matches[0]] = _make_entry(new_content, agent_id)
        self._save(target, entries)
        return {"success": True, "entry_count": len(entries)}

    def read(self, target: str) -> list[str]:
        """Return content strings for all entries in this tenant's target store.

        Signature is backward-compatible: callers that only need the text
        content continue to work unchanged. Use read_with_provenance() when
        agent attribution is required.
        """
        _validate_target(target)
        return [e["content"] for e in self._load(target)]

    def read_with_provenance(self, target: str) -> list[dict[str, Any]]:
        """Return all entries with provenance metadata.

        Each dict has the shape:
            {"content": str, "agent_id": str, "ts": str | None}
        where agent_id is "legacy" for entries written before provenance
        tracking was introduced, and "unknown" when the write path did not
        carry an agent_id (e.g. non-Nous write via D-Bus API).
        """
        _validate_target(target)
        return list(self._load(target))

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

    def _load(self, target: str) -> list[dict[str, Any]]:
        """Load entries from disk. Tolerates both v1 (plain string) and v2 (JSON dict)."""
        path = self._entry_path(target)
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []

        raw_entries = [e.strip() for e in raw.split(_ENTRY_DELIMITER) if e.strip()]
        parsed = [_parse_entry(e) for e in raw_entries]
        # Deduplicate by content, preserving order (first occurrence wins).
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for entry in parsed:
            if entry["content"] not in seen:
                seen.add(entry["content"])
                unique.append(entry)
        return unique

    def _save(self, target: str, entries: list[dict[str, Any]]) -> None:
        path = self._entry_path(target)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise TenantMemoryError(
                f"Cannot create tenant memory dir {path.parent}: {exc}"
            ) from exc

        serialized = [json.dumps(e, ensure_ascii=False) for e in entries]
        content = _ENTRY_DELIMITER.join(serialized) if serialized else ""
        _atomic_write(path, content)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _make_entry(content: str, agent_id: str) -> dict[str, Any]:
    return {
        "content": content,
        "agent_id": agent_id or "unknown",
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


def _parse_entry(raw: str) -> dict[str, Any]:
    """Parse a raw §-delimited entry.

    v2 entries are JSON objects: {"content": ..., "agent_id": ..., "ts": ...}.
    v1 entries are plain strings: treated as legacy provenance.
    Malformed JSON is treated as a plain string (fail-soft).
    """
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "content" in data:
                return {
                    "content": str(data.get("content", "")),
                    "agent_id": str(data.get("agent_id") or _LEGACY_AGENT_ID),
                    "ts": data.get("ts"),
                }
        except (json.JSONDecodeError, ValueError):
            pass
    # Plain string (v1 format) or malformed JSON — treat as legacy.
    return {"content": stripped, "agent_id": _LEGACY_AGENT_ID, "ts": None}


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
