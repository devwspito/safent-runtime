"""hermes.memory — Agent memory subsystem (F4).

Governed, tenant-scoped persistent memory for the agent.

Memory Classification Decision (F4):
  The Nous `memory` tool writes to MEMORY.md / USER.md — agent self-notes,
  not user PII, not external data. These are internal agent notes (procedural
  observations, environment facts, user preferences the agent learned).

  Classification: LOW + auto_executable (NOT HITL for every write).

  Rationale:
  - Reversible: the agent can remove/replace entries. No external effect.
  - Confinement: all writes are scoped to `tenant_id`. A separate SQLite
    store per tenant prevents cross-tenant leakage (multi-tenant isolation).
  - PII policy: memory entries are scanned for PII patterns at write time.
    Any entry matching the PII scanner is REJECTED (fail-closed). The agent
    may only persist tokenized references (e.g., <USER_EMAIL_0>), not raw PII.
  - No external channel: memory writes do not touch the network, filesystem
    outside the confinement root, or any SO surface.

  Kept as NousRisk.WRITE in nous_tool_risk_map.py (WRITE → broker dispatch),
  BUT the broker CapabilityRegistry entry for `memory` is LOW + auto_executable
  so the broker executes it natively without HITL. The broker dispatch still
  audits the write and enforces the tenant confinement.

  The two conditions from the brief are met:
  (a) Store is confined by tenant_id — MultiTenantMemoryStore scopes every
      path under <root>/<tenant_id>/. No path traversal; no cross-tenant read.
  (b) PII is blocked at write: _scan_memory_content uses the same threat-pattern
      scanner as the Nous memory_tool (strict scope).

  If either condition fails at runtime, the write is REJECTED by the broker
  (fail-closed). See application/memory_broker_handler.py.

Persistence:
  The Nous MemoryStore writes to get_hermes_home()/memories/. That path is
  GLOBAL (no multi-tenant). In our confinement layer, we redirect writes to
  /var/lib/hermes/memory/<tenant_id>/ — a per-tenant subtree. The store is
  intentionally NOT readable by other tenants.

  Because Nous memory is currently disabled (skip_memory=True in F1), the
  confinement layer starts by providing a simple per-tenant store that does NOT
  yet bridge to Nous MemoryStore. The bridge requires Nous MemoryStore to accept
  an injected memory_dir — which is feasible but beyond F4 scope. The
  MemoryConfinedStore in this module provides a clean interface for future
  bridging. See infrastructure/tenant_memory_store.py.

GEPA integration:
  The audit log (audit_chain_entries, AuditKind.PROPOSAL_EXECUTED) records
  every tool call + outcome, including memory writes. The GEPA evolution engine
  reads these traces to identify candidate skills without needing special memory
  access. Memory entries themselves are not exposed to GEPA (separation of
  agent-state from evolution inputs).
"""
