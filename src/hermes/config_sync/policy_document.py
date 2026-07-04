"""PolicyBundle — the signed contract the cloud pushes to each associate.

Wire format (JSON) — what the cloud endpoint returns:

{
  "version": 42,
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "issued_at": "2026-06-26T10:00:00Z",
  "signature_hex": "<128-char Ed25519 hex>",
  "payload": { ... }
}

SIGNING FORMAT (P0-1):
  The cloud signs the FULL ENVELOPE — version + tenant_id + issued_at + payload.
  Use `signing_bytes(version, tenant_id, issued_at, payload)` to produce the
  byte sequence that is signed and verified.  Never sign/verify just the payload.

  signing_bytes output shape (JSON, sort_keys, no whitespace, ASCII-safe):
  {
    "issued_at": "...",
    "payload": { ... },
    "tenant_id": "...",
    "version": 42
  }

  `canonical_bytes(payload)` is kept as an internal helper for tests that need
  the payload slice in isolation, but it is NEVER passed to verify_bundle().

CARDINALIY CAPS (P1-3, enforced at Pydantic parse time):
  agents ≤ 200, providers ≤ 50, mcp ≤ 100, skills ≤ 200, consents ≤ 200,
  egress.allow_domains ≤ 500, mcp.env ≤ 100 keys, access_scope.native_tools/
  views ≤ 256 each, access_scope.policy_overlay ≤ 256 keys.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_serializer


# ---------------------------------------------------------------------------
# Access scope spec (Enterprise Fase 2 Phase 3)
# ---------------------------------------------------------------------------


class AccessScopeSpec(BaseModel):
    """Per-agent native-tool access scope pushed by the cloud.

    Wire shape is PINNED — the cloud mirror signs/serializes this exact
    shape; do not add/rename/reorder fields without a coordinated cloud-side
    change. Lands into hermes.capabilities.domain.agent_access_scope.
    AgentAccessScope via the set_agent_access_scope D-Bus verb (see
    hermes/config_sync/applier.py and
    hermes/agents_os/infrastructure/dbus_runtime_service.py).

      enforced:              bool  — default False (governs nothing until True)
      cerebro_unrestricted:  bool  — default True (only bites when enforced +
                                     the agent is the CEO/Cerebro)
      native_tools:          list[str] — allow-set of native tool names, SORTED
      policy_overlay:        dict  — {tool_name: {"enabled": bool}}
      views:                 list[str] — carried; enforcement is a later phase
    """

    enforced: bool = False
    cerebro_unrestricted: bool = True
    native_tools: list[str] = Field(default_factory=list, max_length=256)
    policy_overlay: dict[str, dict[str, bool]] = Field(default_factory=dict)
    views: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("native_tools")
    @classmethod
    def _sorted_unique_native_tools(cls, v: list[str]) -> list[str]:
        """Canonicalise the allow-set: sorted + de-duplicated (it is a set on the
        wire). MUST match the cloud mirror's _sorted_unique_native_tools so
        signing_bytes are byte-identical regardless of the authoring order — the
        byte-mirror must not depend on the signer happening to pre-sort."""
        return sorted(set(v))

    @field_validator("policy_overlay")
    @classmethod
    def _cap_policy_overlay(cls, v: dict) -> dict:
        if len(v) > 256:
            raise ValueError(f"policy_overlay exceeds 256 keys (got {len(v)})")
        return v


# ---------------------------------------------------------------------------
# Agent spec
# ---------------------------------------------------------------------------


class AgentSpec(BaseModel):
    """Cloud-managed agent descriptor.  provider_alias added in Fase 3c."""

    model_config = {"populate_by_name": True}

    agent_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=512)
    # Field stored as register_tone to avoid shadowing pydantic's BaseModel.register.
    # Wire name (alias) remains "register" for backward compat with the cloud contract.
    register_tone: str = Field(default="", max_length=512, alias="register")
    primary_mission: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=8000)
    language: str = Field(default="auto", max_length=20)
    color: str = Field(default="#6366f1", max_length=30)
    golden_rules: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    autonomy_level: str = Field(default="balanced", max_length=32)
    department: str | None = Field(default=None, max_length=64)
    provider_alias: str | None = Field(default=None, max_length=120)
    # Capability bindings: {"kind": "skill"|"mcp", "id": "...", "version": ""}
    capabilities: list[dict[str, str]] = Field(default_factory=list)
    # Enterprise Fase 2 Phase 3: per-agent native-tool access scope. None/absent
    # -> agent unscoped (today's behaviour, zero regression).
    access_scope: AccessScopeSpec | None = None

    @model_serializer(mode="wrap")
    def _serialize_agent(self, handler: Any) -> dict[str, Any]:
        """Drop access_scope from the dump when unset (back-compat bytes).

        AgentSpec predates access_scope (Fase 2 Phase 3): a bundle without it
        must sign/serialize BYTE-IDENTICALLY to before this field existed, so
        `"access_scope":null` must never appear when it is None.
        """
        data = handler(self)
        if self.access_scope is None:
            data.pop("access_scope", None)
        return data


# ---------------------------------------------------------------------------
# Provider spec
# ---------------------------------------------------------------------------


class ProviderSpec(BaseModel):
    """Cloud-managed LLM provider.

    base_url is validated against the SSRF blocklist in the applier (P2).
    api_key is optional — present when the enterprise configures the provider
    key centrally.  It travels in the Ed25519-signed bundle over HTTPS and is
    stored in the associate's SecretsVault on landing.  NEVER logged.
    """

    alias: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=64)
    default_model: str = Field(min_length=1, max_length=256)
    base_url: str | None = Field(default=None, max_length=1024)
    set_active: bool = False
    # Enterprise-supplied API key — signed by the cloud, stored in vault on landing.
    api_key: str | None = Field(default=None, max_length=512)


# ---------------------------------------------------------------------------
# Integration spec (Composio key push)
# ---------------------------------------------------------------------------


class IntegrationSpec(BaseModel):
    """Cloud-managed integration credential.

    kind is always "composio" in Fase 4.  The api_key is covered by the
    Ed25519 signature.  The applier never logs the key value (P0-4).
    managed_by tracking for integrations is a Fase 5 follow-up (see applier.py).
    """

    kind: str = Field(min_length=1, max_length=64)  # "composio"
    api_key: str = Field(min_length=1, max_length=512)
    entity_id: str = Field(default="default", max_length=128)


# ---------------------------------------------------------------------------
# MCP spec
# ---------------------------------------------------------------------------


class McpSpec(BaseModel):
    """Cloud-managed MCP server."""

    server_id: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=120)
    argv: list[str] = Field(default_factory=list)
    # P1-3: cap env at 100 keys.
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env")
    @classmethod
    def _cap_env(cls, v: dict) -> dict:
        if len(v) > 100:
            raise ValueError(f"mcp.env exceeds 100 keys (got {len(v)})")
        return v


# ---------------------------------------------------------------------------
# Skill spec
# ---------------------------------------------------------------------------


class SkillSpec(BaseModel):
    """Cloud-managed hub skill (installed by identifier)."""

    identifier: str = Field(min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# Egress spec
# ---------------------------------------------------------------------------


class EgressSpec(BaseModel):
    """Network egress additions.

    The applier ONLY adds domains to the allow-list on top of existing owner
    grants.  It never removes owner-granted domains, never touches the
    blocklist, and never switches the network mode.
    P1-3: capped at 500 domains.
    """

    allow_domains: list[str] = Field(default_factory=list, max_length=500)


# ---------------------------------------------------------------------------
# Consent spec
# ---------------------------------------------------------------------------


class ConsentSpec(BaseModel):
    """Cloud-managed consent grant.

    HIGH-risk consents (terminal_exec, file_write, …) are PROPOSED but not
    auto-approved — the applier skips them and records the gap in ApplyResult.
    """

    capability: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="session", max_length=64)
    granted_through: str = Field(default="cloud_sync", max_length=128)


# ---------------------------------------------------------------------------
# Features spec
# ---------------------------------------------------------------------------


class FeaturesSpec(BaseModel):
    """Feature flags / UI-view entitlements."""

    views: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# License spec
# ---------------------------------------------------------------------------


class LicenseSpec(BaseModel):
    """License entitlements (stored in association_store.license_json).

    `remote_approval_enabled` (Fase 2 Phase 4b): tenant-level gate for the
    Enterprise remote-approval feature — `security_hook._tenant_remote_approval_
    enabled()` reads it back from `association_store.license_json` (the SAME
    field this model serializes into, via `store.update_license()`; no new
    D-Bus verb, mirrors how `views` already travels — see applier.py's NOTE).
    Default False is BACK-COMPAT: dropped from the dump when False (see
    `_serialize_license` below) so a bundle from a cloud that predates this
    field signs BYTE-IDENTICALLY to before it existed — mirrors
    AgentSpec.access_scope's drop-when-absent pattern.
    """

    plan: str = Field(default="starter", max_length=64)
    max_agents: int = Field(default=5, ge=0)
    expires_at: str = Field(default="", max_length=32)  # ISO-8601 date or ""
    views: list[str] = Field(default_factory=list)
    remote_approval_enabled: bool = False

    @model_serializer(mode="wrap")
    def _serialize_license(self, handler: Any) -> dict[str, Any]:
        """Drop remote_approval_enabled from the dump when False (back-compat bytes).

        LicenseSpec predates this field: a bundle that never sets it must sign/
        serialize BYTE-IDENTICALLY to before the field existed, so
        `"remote_approval_enabled":false` must never appear when it is unset/False.
        """
        data = handler(self)
        if self.remote_approval_enabled is False:
            data.pop("remote_approval_enabled", None)
        return data


# ---------------------------------------------------------------------------
# Top-level payload and bundle
# ---------------------------------------------------------------------------


class PolicyPayload(BaseModel):
    """The signable body of the bundle (everything except the signature)."""

    # P1-3 cardinality caps enforced at parse time.
    agents: list[AgentSpec] = Field(default_factory=list, max_length=200)
    providers: list[ProviderSpec] = Field(default_factory=list, max_length=50)
    integrations: list[IntegrationSpec] = Field(default_factory=list, max_length=50)
    mcp: list[McpSpec] = Field(default_factory=list, max_length=100)
    skills: list[SkillSpec] = Field(default_factory=list, max_length=200)
    egress: EgressSpec = Field(default_factory=EgressSpec)
    consents: list[ConsentSpec] = Field(default_factory=list, max_length=200)
    features: FeaturesSpec = Field(default_factory=FeaturesSpec)
    license: LicenseSpec = Field(default_factory=LicenseSpec)


class PolicyBundle(BaseModel):
    """The full signed bundle delivered by the cloud endpoint."""

    version: int = Field(ge=0)
    tenant_id: str = Field(min_length=1, max_length=128)
    # issued_at is normalised to "YYYY-MM-DDTHH:MM:SSZ" (no fractions, trailing Z)
    # by the cloud before signing so that signing_bytes is byte-identical on both sides.
    issued_at: str = Field(min_length=1, max_length=64)
    signature_hex: str = Field(min_length=128, max_length=128)
    payload: PolicyPayload


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def canonical_bytes(payload: PolicyPayload) -> bytes:
    """Payload-only canonical encoding (internal helper / test utility).

    NOT used for signature verification — use signing_bytes() for that.
    Kept for test isolation and internal use.
    """
    raw: dict[str, Any] = payload.model_dump()
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def signing_bytes(
    *,
    version: int,
    tenant_id: str,
    issued_at: str,
    payload: PolicyPayload,
) -> bytes:
    """Return the deterministic bytes the cloud signs (and the associate verifies).

    P0-1: Signs the FULL ENVELOPE — version + tenant_id + issued_at + payload —
    so that mutating any envelope field (replay with a different version, tenant
    swap, timestamp rollback) invalidates the signature.

    Encoding rules (same as canonical_bytes):
    - sort_keys=True at every nesting level
    - separators=(',', ':') — no extra whitespace
    - ensure_ascii=True — encoding-agnostic across platforms
    - encode('ascii') — byte-identical on both sides of the wire

    Wire format of the signed envelope (what the cloud must sign):
    {
      "issued_at": "...",
      "payload":   { <PolicyPayload fields, sorted> },
      "tenant_id": "...",
      "version":   42
    }

    Test vector (committed):
      See tests/unit/config_sync/test_policy_document.py::TestSigningBytes
      for a stable byte-sequence fixture that pins the format.
    """
    envelope: dict[str, Any] = {
        "version": version,
        "tenant_id": tenant_id,
        "issued_at": issued_at,
        "payload": payload.model_dump(),
    }
    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
