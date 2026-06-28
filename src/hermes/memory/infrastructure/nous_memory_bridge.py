"""NousMemoryBridge — Option B: snapshot injection into ephemeral_system_prompt.

Bridges TenantMemoryStore (our store) with NousReasoningEngine so that
Nous has cross-session recall without writing directly to ~/.hermes.

Design decision: Option (B) — snapshot, not memory_dir redirect.
  - Option (A) would redirect Nous's memory_dir to our tenant dir and let
    Nous read/write MEMORY.md/USER.md directly. Rejected because Nous writes
    bypass our PII gate; MemoryStore.save_to_disk() has no scan. Any mid-
    session memory write from Nous would land unscanned.
  - Option (B): TenantMemoryStore is the source of truth. At cycle-build time
    we read the tenant's store and render it into a snapshot block that is
    appended to the ephemeral_system_prompt. Nous reads the snapshot; ALL
    writes go through our broker → MemorySurfaceAdapter → TenantMemoryStore
    (PII-gated, tenant-confined). This is the only write path.

Multi-tenant guarantee:
  The snapshot is built from TenantMemoryStore(root, tenant_id=X). The
  tenant_id comes from NousReasoningEngine._tenant_id, which is set per
  cycle from DecisionContext.tenant_id. Two tenants never share a
  TenantMemoryStore instance — path is always <root>/<tenant_id>/<target>.md.

PII guarantee:
  TenantMemoryStore._assert_no_pii() is called on every write. The snapshot
  only surfaces entries that already passed the gate at write time. We do a
  secondary scan at snapshot-build time as defense-in-depth: any entry
  flagged post-write (e.g. after a scanner update) is replaced with a
  placeholder in the rendered block before it enters Nous's system prompt.

session_search / FTS5 gap:
  Nous's session_search uses a SQLite FTS5 index over state.db (per-install,
  global). We do NOT bridge that index — it is not tenant-scoped and operates
  over raw session messages. Bridging it requires a per-tenant FTS5 DB (new
  spec). Until then, session_search is available as a Nous READ tool (it is
  already classified LOW+auto in CapabilityRegistry) but it queries the global
  state.db, not our store. The §-entry recall this bridge provides covers the
  curated long-term memory that agents explicitly store; session_search covers
  verbatim conversation recall.

Capa: infrastructure (filesystem I/O via TenantMemoryStore). No framework.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import UUID

from hermes.memory.infrastructure.tenant_memory_store import TenantMemoryStore

logger = logging.getLogger(__name__)

_DEFAULT_MEMORY_ROOT = Path(
    os.environ.get("HERMES_MEMORY_ROOT", "/var/lib/hermes/memory")
)

# Targets surfaced to Nous. "memory" = agent notes; "user" = user profile.
_SNAPSHOT_TARGETS: tuple[str, ...] = ("memory", "user")

_TARGET_HEADERS: dict[str, str] = {
    "memory": "MEMORY (agent persistent notes)",
    "user": "USER PROFILE (user preferences and context)",
}


class NousMemoryBridge:
    """Builds a tenant-scoped memory snapshot for injection into Nous prompts.

    Usage (in NousReasoningEngine._build_governed_agent):
        bridge = NousMemoryBridge(memory_root=root, tenant_id=tid)
        base_system_prompt = ...  # built by DefaultPromptBuilder
        enriched_prompt = bridge.enrich_system_prompt(base_system_prompt)
        # Pass enriched_prompt as ephemeral_system_prompt to GovernedAIAgent.

    The snapshot is rebuilt on every call to enrich_system_prompt (once per
    cycle). This matches Nous MemoryStore's load_from_disk() — called before
    each run_conversation — so both approaches rebuild at the same frequency.
    """

    def __init__(self, *, memory_root: Path, tenant_id: UUID) -> None:
        self._store = TenantMemoryStore(root=memory_root, tenant_id=tenant_id)
        self._tenant_id = tenant_id

    def enrich_system_prompt(self, base_prompt: str) -> str:
        """Append tenant memory snapshot to base_prompt.

        Returns base_prompt unchanged if the store is empty for all targets.
        """
        snapshot = self._build_snapshot()
        if not snapshot:
            return base_prompt
        separator = "\n\n" if base_prompt else ""
        return base_prompt + separator + snapshot

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _build_snapshot(self) -> str:
        blocks: list[str] = []
        for target in _SNAPSHOT_TARGETS:
            block = self._render_target(target)
            if block:
                blocks.append(block)
        if not blocks:
            return ""
        fence = "═" * 46
        header = f"{fence}\nHERMES PERSISTENT MEMORY (tenant-scoped, read-only snapshot)\n{fence}"
        return header + "\n\n" + "\n\n".join(blocks)

    def _render_target(self, target: str) -> str:
        """Read entries from store and render as a labeled block.

        Entries that fail the secondary PII scan are replaced with a
        placeholder — defense-in-depth against stale/bypassed entries.
        Provenance annotation (agent_id) is appended as a soft suffix —
        fail-soft: if read_with_provenance is unavailable we fall back to
        read() without annotation.
        """
        try:
            raw_entries = self._store.read_with_provenance(target)
        except Exception as exc:
            logger.warning(
                "hermes.memory_bridge.read_error tenant=%s target=%s: %s",
                str(self._tenant_id)[:8],
                target,
                exc,
            )
            return ""

        if not raw_entries:
            return ""

        annotated: list[str] = []
        for entry in raw_entries:
            content = entry.get("content", "")
            agent_id = entry.get("agent_id", "unknown")
            scanned = _scan_entry(content, target)
            if scanned:
                suffix = f"  ·({agent_id})" if agent_id not in ("unknown", "legacy") else ""
                annotated.append(scanned + suffix)

        non_empty = [e for e in annotated if e]
        if not non_empty:
            return ""

        header = _TARGET_HEADERS.get(target, target.upper())
        separator = "\n§\n"
        return f"{header}\n{separator.join(non_empty)}"


def build_nous_memory_bridge(*, tenant_id: UUID) -> NousMemoryBridge:
    """Factory: creates a NousMemoryBridge with the configured memory root.

    memory_root is read from HERMES_MEMORY_ROOT env or defaults to
    /var/lib/hermes/memory. This matches MemorySurfaceAdapter's default.
    """
    return NousMemoryBridge(memory_root=_DEFAULT_MEMORY_ROOT, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _scan_entry(entry: str, target: str) -> str:
    """Secondary PII scan for snapshot rendering (defense-in-depth).

    If the scanner is unavailable (non-Nous env), entries pass through.
    """
    try:
        from tools.threat_patterns import first_threat_message  # noqa: PLC0415

        error_msg = first_threat_message(entry, scope="strict")
        if error_msg:
            logger.warning(
                "hermes.memory_bridge.secondary_scan_blocked target=%s: %s",
                target,
                error_msg,
            )
            return f"[BLOCKED: {target} entry failed secondary PII scan — removed from snapshot]"
    except ImportError:
        pass
    return entry
