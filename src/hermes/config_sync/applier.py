"""PolicyApplier — declarative reconcile of cloud policy onto the associate.

The applier is a D-Bus client: it calls the SAME verbs the shell-server REST
routers call, so there is no duplicated write logic.  The runtime daemon
remains the single writer of all state.

Application order (dependency graph):
  1. providers     — agents reference provider_alias
  2. integrations  — (composio key; stateless, no deps)
  3. mcp           — skills and agents can reference MCP servers
  4. skills        — agents can bind skills as capabilities
  5. agents        — upserts only (no deletes yet — P1-4)
  6. consents      — capability grants (HIGH consents require human approval)
  7. egress        — only ADDS to allow-list; never removes owner grants
  8. license       — persisted in association_store
  9. directory     — Fase 3 department-scoped visibility; persisted in
                      association_store (no D-Bus verb — read-only
                      presentation data, see _apply_directory)
  10. DELETE stale cloud-managed agents (P1-4: AFTER all upserts succeed)

  NOTE — features/views:
  Feature-view entitlements travel in license.views (LicenseSpec) which is
  persisted by __main__.py via store.update_license().  The feature_guard
  middleware reads license["views"] from the association_store directly —
  no D-Bus verb (set_feature_flags) is needed.  FeaturesSpec in the bundle
  is kept for wire-compat but is NOT applied here.

  NOTE — directory (Fase 3):
  Like license, the directory has no D-Bus verb: _apply_directory persists
  it straight into association_store via the injected directory_store
  (SQLiteAssociationStore.update_directory).  roster_api and
  DelegationSurfaceAdapter read it back from the same store.  A directory
  persistence failure is logged, never marks the section failed (this is
  presentation data, not a security control — the cloud enforces the
  department gate authoritatively on delegation).

P0-3 — D-Bus verb allow-list:
  Only verbs in _ALLOWED_VERBS may be called by config-sync.  Any attempt to
  call a verb not on the list fails loudly (logged + marked as failure) and the
  call is NEVER made.  This is default-deny for the D-Bus surface.

  Daemon-side note: add_egress_domain, set_feature_flags, update_provider MUST,
  when implemented in the daemon, enforce sovereignty constraints there too.
  The applier's allow-list is a defence-in-depth guard, not the only gate.

Sovereignty invariants (non-negotiable):
  - Egress: ONLY adds domains from the allow-list; never removes owner domains;
    never changes mode; never touches the blocklist.  Each domain is validated
    against the same _DOMAIN_RE regex that the REST API uses (P0-3).
  - HIGH-risk consents (terminal_exec, file_write, …) are NOT auto-approved.
  - No verb that widens the kernel cage or disables the blocklist is permitted.

P0-4 — integration managed_by:
  The composio API key is only pushed when there is no existing local (non-cloud)
  integration already configured.  managed_by tracking for the integrations table
  is a Fase 5 follow-up (the daemon's integration store doesn't have the column).
  In Fase 4: we write the key only if the daemon returns has_key=False (no
  pre-existing key).  Never log the api_key value.

P1-4 — deletes after upserts:
  Cloud-managed agent deletes run ONLY after all upsert phases (providers →
  integrations → mcp → skills → agents upsert → consents → egress → features)
  have completed without failures.  If any upsert fails, deletes are skipped
  and last_applied_version is NOT advanced.

2026-07-07 — MCP capability authorization via AgentAccessScope, not binding:
  bind_capability_to_agent is D-Bus-DENIED for config-sync's uid=hermes by
  design (CWE-441 confused-deputy fix, org.hermes.Runtime1.conf) — it can
  never succeed from this process. An agent's kind="mcp" capabilities are
  therefore never bound via that verb; instead they land on the agent's
  AgentAccessScope (authorized_mcp_servers) through the ALLOWED
  set_agent_access_scope verb — see _bind_non_mcp_capabilities and
  _apply_access_scope. nous_engine._filter_mcp_skill reads that set for its
  fail-closed MCP admission. Non-MCP capabilities (skill, ...) still attempt
  bind_capability_to_agent for back-compat, but a failure there is logged and
  NEVER blocks version advancement (the verb is structurally unreachable from
  config-sync, so requiring its success would deadlock every sync forever).

2026-07-07 — bundle-sourced MCP servers vs the Security Center WARN gate:
  Every MCP server _apply_mcp processes comes from the Ed25519 tenant-signed
  bundle (config-sync only ever calls apply() with a verified PolicyPayload)
  — the tenant signature IS the authority, so it is owner-authored and
  trusted by construction, unlike an interactive/agent-initiated add_mcp_server
  call from inside the instance. add_mcp_server's Security-Center gate treats
  a discretionary WARN verdict (score 40-69) the same as a hard FAIL by
  default: {"ok": False, "blocked": True, "warn": True, ...}. Previously
  _apply_mcp classified that shape as a transitory `failed`, which meant
  hermes.config_sync.partial_failure fired forever and last_applied_version
  never advanced — the WHOLE bundle got stuck, not just the MCP section.

  Fix: _apply_mcp distinguishes the WARN shape (_is_discretionary_warn_block,
  keyed on the "warn" flag that only that branch sets) from a genuine FAIL /
  scanner-error block (_is_permanent_rejection — no "warn" key). Only the
  WARN case is retried with the daemon's existing owner-override plumbing
  (add_mcp_server(force=True), the same mechanism the interactive UI uses
  after an MFA-approved override) — the signed bundle stands in for that
  approval. A FAIL / scanner-error block is NEVER retried with force=True;
  it is recorded in `result.rejected` (permanent, like _apply_skills) so it
  is surfaced/logged and does not stall version advancement, but the server
  stays uninstalled. Locally/agent-initiated MCP installs never go through
  this code path — add_mcp_server's own default (force=False, entered
  directly by the UI/D-Bus caller) keeps blocking WARN for that path.

P2 — strict _is_ok for sensitive sections:
  For egress, consents, and integrations, "ok" must be explicitly True.
  An empty dict / None is treated as failure (not silent success).

Follow-ups deferred to Fase 5 / Fase 7:
  - managed_by on providers, integrations, mcp (the daemon stores lack the column).
  - Skill uninstall (declarative delete of cloud-managed skills).
  - mTLS transport (Fase 7 cloud endpoint).
  - NODE_ENROLLMENT HMAC binding (Fase 7, both sides change together).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from hermes.config_sync.policy_document import (
    AgentSpec,
    ConsentSpec,
    DirectorySpec,
    EgressSpec,
    IntegrationSpec,
    LicenseSpec,
    McpSpec,
    PolicyPayload,
    ProviderSpec,
    SkillSpec,
)

logger = logging.getLogger("hermes.config_sync.applier")

# ---------------------------------------------------------------------------
# P0-3: D-Bus verb allow-list (default-deny)
# ---------------------------------------------------------------------------

_ALLOWED_VERBS: frozenset[str] = frozenset(
    {
        # Read verbs (safe to call anytime)
        "list_agents",
        "list_providers",
        "list_mcp_servers",
        "list_consents",
        "list_egress_grants",
        # Provider management
        "add_provider",
        "update_provider",
        "delete_provider",  # reconciliation: drop cloud providers absent from bundle
        # Composio integration
        "set_composio_api_key",
        "get_composio_status",
        # MCP server management
        "add_mcp_server",
        # Hub skills
        "install_hub_skill",
        # Agent management
        "create_agent",
        "update_agent",
        "delete_agent",
        # Capability binding
        "bind_capability_to_agent",
        # Consent management (LOW-risk only; HIGH is blocked in _apply_consents)
        "grant_consent",
        # Egress (add domain only — never mode change, never remove)
        "add_egress_domain",
        # Per-agent native-tool access scope (Enterprise Fase 2 Phase 3).
        # clear_agent_access_scope is NOT allow-listed: it has no wiring method
        # nor D-Bus export today (reconcile is additive-only — see
        # _upsert_agent's access_scope handling) — an allow-listed-but-
        # unreachable verb is its own bug class (F2 review fix); add it back
        # only alongside a real implementation on both ends.
        "set_agent_access_scope",
        # NOTE: set_feature_flags is intentionally ABSENT.
        # Feature views travel in license.views (LicenseSpec) and are persisted by
        # __main__.py via store.update_license(). The feature_guard middleware reads
        # them directly from the association_store — no D-Bus verb required.
    }
)

# ---------------------------------------------------------------------------
# P0-3: Egress domain validation (same regex as egress_api.py REST route)
# ---------------------------------------------------------------------------

# Hostname (optionally a leading wildcard, stripped). No scheme, no path, no port.
# Mirrors _DOMAIN_RE in hermes/shell_server/egress_api.py exactly.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)

# ---------------------------------------------------------------------------
# P2: SSRF check for provider base_url
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::/128"),
]
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "metadata"})


def _is_safe_base_url(url: str) -> bool:
    """Return False if url is a private/loopback/metadata address (SSRF guard).

    Mirrors the validation logic in http_control_plane_client._validate_cloud_endpoint.
    Only https:// is allowed; loopback, RFC1918, link-local, and metadata blocked.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            return False
        try:
            addr = ipaddress.ip_address(hostname)
            return not any(addr in net for net in _BLOCKED_NETWORKS)
        except ValueError:
            return True  # hostname is a domain name; allowed
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Consents
# ---------------------------------------------------------------------------

_HIGH_RISK_CONSENT_PREFIXES: frozenset[str] = frozenset(
    {
        "terminal_exec",
        "file_write",
        "process_spawn",
        "camera",
        "microphone",
        "credential_store",
    }
)

_CLOUD_MANAGED = "cloud"

# Capability kind that is authorized via AgentAccessScope.authorized_mcp_servers
# (set_agent_access_scope), NOT via bind_capability_to_agent — see
# PolicyApplier._bind_non_mcp_capabilities / _apply_access_scope.
_MCP_CAPABILITY_KIND = "mcp"

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of one policy application pass.

    `ok` is True only if zero entities had TRANSITORY failures.
    The sync loop advances `last_applied_version` when `ok` is True.

    `rejected` holds permanent policy/security rejections (e.g. a skill
    blocked by the Security Center scan verdict FAIL).  These are NOT
    retried and do NOT block version advancement — a permanently-rejected
    entity will always be rejected on the next attempt, so preventing the
    version from advancing would loop forever.

    Invariant: a permanent rejection NEVER causes the blocked entity to be
    installed.  Only `failed` (transitory) prevents version advancement.
    """

    applied: int = 0
    failed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when there are no TRANSITORY failures (version can advance)."""
        return len(self.failed) == 0


# ---------------------------------------------------------------------------
# Directory store protocol (Fase 3 — department-scoped visibility)
# ---------------------------------------------------------------------------


class _DirectoryStoreProtocol(Protocol):
    """Duck-typed dependency — satisfied by SQLiteAssociationStore.

    Mirrors uploader.py's _AssociationStoreProtocol pattern: the applier
    depends on the narrow slice it needs, not the concrete store class.
    """

    def update_directory(self, directory: dict | None) -> None:
        ...


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------


class PolicyApplier:
    """Applies a PolicyPayload to the local associate via the D-Bus proxy.

    The proxy is injected so tests can pass a FakeDbusProxy without D-Bus.

    directory_store (Fase 3, optional): where the delivered department
    directory is persisted. Unlike agents/providers/etc. this section has
    NO D-Bus verb — it is read-only presentation data (the cloud already
    enforces the department gate authoritatively on delegation), so it is
    persisted the SAME way license.views already is: directly into
    SQLiteAssociationStore, no daemon round-trip. None (the default) makes
    `_apply_directory` a no-op — existing callers/tests that don't inject a
    store are unaffected.
    """

    def __init__(
        self, proxy: Any, *, directory_store: _DirectoryStoreProtocol | None = None
    ) -> None:
        self._proxy = proxy
        self._directory_store = directory_store

    async def apply(
        self,
        payload: PolicyPayload,
        *,
        current_agents: list[dict] | None = None,
        tenant_id: str = "",
    ) -> ApplyResult:
        """Reconcile all sections in dependency order (P1-4: deletes at end).

        tenant_id: the bundle envelope's tenant_id (PolicyBundle.tenant_id),
        forwarded to set_agent_access_scope (Enterprise Fase 2 Phase 3). Default
        "" preserves existing callers that don't scope agent access yet.
        """
        result = ApplyResult()

        # Phase 1: upsert everything
        await self._apply_providers(payload.providers, result)
        await self._apply_integrations(payload.integrations, result)
        await self._apply_mcp(payload.mcp, result)
        await self._apply_skills(payload.skills, result)

        live_agents = current_agents
        if live_agents is None:
            live_agents = await self._fetch_agents()
        cloud_managed = _cloud_managed_index(live_agents)
        await self._upsert_agents(payload.agents, cloud_managed, result, tenant_id)

        await self._apply_consents(payload.consents, result)
        await self._apply_egress(payload.egress, result)
        # NOTE: features.views is NOT applied here via D-Bus.  Feature-view
        # entitlements travel in license.views (LicenseSpec) and are persisted
        # by __main__.py via store.update_license() immediately after apply()
        # returns ok.  The feature_guard middleware reads them directly from the
        # association_store — no separate D-Bus verb is needed or correct.
        self._validate_license(payload.license)
        self._apply_directory(payload.directory)

        # Phase 2: DELETE stale cloud-managed agents — only if all upserts ok.
        # If any upsert failed we stop here; the sync loop will not advance the
        # version and will retry on the next tick (P1-4).
        if result.ok:
            bundle_ids = {s.agent_id for s in payload.agents}
            await self._delete_stale_agents(cloud_managed, bundle_ids, result)

        return result

    # ------------------------------------------------------------------
    # Section appliers
    # ------------------------------------------------------------------

    async def _apply_providers(
        self, providers: list[ProviderSpec], result: ApplyResult
    ) -> None:
        """Upsert cloud providers, then reconcile (the cloud owns the set).

        P2: base_url validated against SSRF blocklist before calling the daemon.

        UpdateProvider requires (provider_id, draft_json): the existing list is
        indexed by alias to retrieve the provider_id before calling the verb.

        Reconciliation: any provider previously stamped managed_by="cloud" whose
        alias is no longer in the bundle is deleted — so removing a provider in
        the console removes it on the associate. Locally-owned providers
        (managed_by=None) are never touched.
        """
        existing = await self._proxy.call_list("list_providers")
        existing_by_alias = {p.get("alias", ""): p for p in existing}

        for spec in providers:
            if spec.base_url and not _is_safe_base_url(spec.base_url):
                logger.warning(
                    "hermes.config_sync.applier.provider_unsafe_base_url",
                    extra={"alias": spec.alias},
                )
                result.failed.append(f"provider:{spec.alias}:unsafe_base_url")
                continue

            draft = _provider_draft(spec)
            if spec.alias not in existing_by_alias:
                resp = await self._call_mutator("add_provider", json.dumps(draft))
            else:
                provider_id = existing_by_alias[spec.alias].get("provider_id", "")
                resp = await self._call_mutator(
                    "update_provider", provider_id, json.dumps(draft)
                )

            if _is_ok_lenient(resp):
                result.applied += 1
            else:
                result.failed.append(f"provider:{spec.alias}")

        # Reconcile: drop cloud-managed providers the bundle no longer lists.
        bundle_aliases = {spec.alias for spec in providers}
        for p in existing:
            if p.get("managed_by") != "cloud" or p.get("alias", "") in bundle_aliases:
                continue
            pid = p.get("provider_id", "")
            resp = await self._call_mutator("delete_provider", pid)
            if _is_ok_lenient(resp):
                result.applied += 1
            else:
                result.failed.append(f"provider:delete:{p.get('alias', '')}")

    async def _apply_integrations(
        self, integrations: list[IntegrationSpec], result: ApplyResult
    ) -> None:
        """Push Composio API key.

        P0-4: Only push the key when no local key exists (has_key=False from the
        daemon), to avoid overwriting a locally-configured key with a cloud value.
        managed_by tracking for integrations is a Fase 5 follow-up.

        Security: the api_key is NEVER logged; only its presence is logged.
        """
        for spec in integrations:
            if spec.kind != "composio":
                logger.warning(
                    "hermes.config_sync.applier.unknown_integration_kind",
                    extra={"kind": spec.kind},
                )
                result.failed.append(f"integration:{spec.kind}")
                continue

            # Check for existing local (non-cloud) key before overwriting.
            # KNOWN GAP (backlog): the daemon's get_composio_status returns only
            # {configured, entity_id} — no has_key/managed_by — so this guard is
            # currently dead and the cloud key always overwrites. Honoring "don't
            # clobber a local key" needs a managed_by column on the integration
            # store (Fase 5); left as-is to preserve cloud key-rotation until then.
            status = await self._proxy.call_dict("get_composio_status")
            if status.get("has_key") and not status.get("managed_by") == _CLOUD_MANAGED:
                # A locally-configured key exists; do not overwrite it.
                logger.info(
                    "hermes.config_sync.applier.integration_skipped_local_key",
                    extra={"kind": spec.kind},
                )
                result.applied += 1
                continue

            # Never log spec.api_key — log only that we are pushing it.
            logger.info(
                "hermes.config_sync.applier.integration_pushing_key",
                extra={"kind": spec.kind},
            )
            resp = await self._call_mutator("set_composio_api_key", spec.api_key)
            if _is_ok_strict(resp):
                result.applied += 1
            else:
                result.failed.append(f"integration:{spec.kind}")

    async def _apply_mcp(
        self, servers: list[McpSpec], result: ApplyResult
    ) -> None:
        """Upsert MCP servers.  No delete in Fase 4 (follow-up: managed_by).

        See the module-level 2026-07-07 note for why a discretionary Security-
        Center WARN on a bundle-sourced server is retried with force=True
        instead of stalling last_applied_version — _apply_one_mcp_server does
        the classification.
        """
        existing = await self._proxy.call_list("list_mcp_servers")
        existing_ids = {s.get("server_id", "") for s in existing}

        for spec in servers:
            if spec.server_id in existing_ids:
                # No update verb for MCP; skip (idempotent).
                result.applied += 1
                continue
            await self._apply_one_mcp_server(spec, result)

    async def _apply_one_mcp_server(self, spec: McpSpec, result: ApplyResult) -> None:
        """Install one bundle-sourced MCP server, classifying the verdict.

        - ok                              → applied.
        - discretionary WARN block        → retried with force=True (bundle
                                             signature stands in for the
                                             owner's MFA override); ok on
                                             retry → applied, still blocked
                                             on retry → falls through below.
        - FAIL / scanner-error block       → rejected (permanent, logged,
                                             does NOT stall version advance).
        - anything else (network, daemon
          down, unknown error)             → failed (transitory, retried
                                             next sync tick).
        """
        resp = await self._add_mcp_server(spec, force=False)
        if _is_ok_lenient(resp):
            result.applied += 1
            return
        if _is_discretionary_warn_block(resp):
            resp = await self._retry_mcp_bundle_warn_override(spec, resp)
            if _is_ok_lenient(resp):
                result.applied += 1
                return
        if _is_permanent_rejection(resp):
            logger.warning(
                "hermes.config_sync.applier.mcp_permanently_rejected",
                extra={"server_id": spec.server_id, "warn": bool(resp.get("warn"))},
            )
            result.rejected.append(f"mcp:{spec.server_id}:scan_blocked")
            return
        result.failed.append(f"mcp:{spec.server_id}")

    async def _retry_mcp_bundle_warn_override(
        self, spec: McpSpec, warn_resp: dict
    ) -> dict:
        """Bundle-sourced MCP + discretionary WARN: retry via the daemon's
        existing owner-override plumbing (force=True). Only reached when the
        first response was specifically a WARN block (see
        _is_discretionary_warn_block) — a genuine FAIL/hard verdict never
        enters this path.
        """
        logger.warning(
            "hermes.config_sync.applier.mcp_bundle_warn_override",
            extra={"server_id": spec.server_id, "score": warn_resp.get("score")},
        )
        return await self._add_mcp_server(spec, force=True)

    async def _add_mcp_server(self, spec: McpSpec, *, force: bool) -> dict:
        draft = {
            "server_id": spec.server_id,
            "label": spec.label or spec.server_id,
            "argv": spec.argv,
            "env": spec.env,
            "force": force,
        }
        return await self._call_mutator("add_mcp_server", json.dumps(draft))

    async def _apply_skills(
        self, skills: list[SkillSpec], result: ApplyResult
    ) -> None:
        """Install hub skills (idempotent; daemon is no-op if already installed).

        Permanent rejection: the daemon returns {"ok": False, "blocked": True, ...}
        when the Security Center scan verdict is FAIL and auto_block_fail=True.
        This is a sovereignty decision — the install MUST NOT happen and retrying
        is pointless.  Record it as `rejected` so it does not block version advancement.

        Transitory failure: any other ok:false response (daemon down, network,
        unknown error) is recorded as `failed` and will be retried.
        """
        for spec in skills:
            resp = await self._call_mutator("install_hub_skill", spec.identifier, False)
            if _is_ok_lenient(resp):
                result.applied += 1
            elif _is_permanent_rejection(resp):
                logger.warning(
                    "hermes.config_sync.applier.skill_permanently_rejected",
                    extra={"identifier": spec.identifier},
                )
                result.rejected.append(f"skill:{spec.identifier}:scan_blocked")
            else:
                result.failed.append(f"skill:{spec.identifier}")

    async def _upsert_agents(
        self,
        specs: list[AgentSpec],
        cloud_managed: dict[str, dict],
        result: ApplyResult,
        tenant_id: str,
    ) -> None:
        """Upsert-only phase.  Deletes happen separately after all upserts (P1-4)."""
        for spec in specs:
            ok = await self._upsert_agent(spec, cloud_managed, tenant_id)
            if ok:
                result.applied += 1
            else:
                result.failed.append(f"agent:{spec.agent_id}")

    async def _delete_stale_agents(
        self,
        cloud_managed: dict[str, dict],
        bundle_ids: set[str],
        result: ApplyResult,
    ) -> None:
        """Delete cloud-managed agents absent from the bundle (P1-4: runs last)."""
        for agent_id in cloud_managed:
            if agent_id not in bundle_ids:
                ok = await self._delete_agent(agent_id)
                if ok:
                    result.applied += 1
                else:
                    result.failed.append(f"agent:delete:{agent_id}")

    async def _apply_consents(
        self, consents: list[ConsentSpec], result: ApplyResult
    ) -> None:
        """Grant LOW-risk consents.

        Consent semantics (sovereignty model):
        - HIGH-risk consents (terminal_exec, file_write, …): always operator-gated
          — recorded as pending_operator so the version can still advance.
        - LOW-risk consents: attempted via D-Bus.  Operator-gated failures
          (authorization/permission denied — by design, as config_sync uid is not
          in authorized_uids for grant_consent) are classified as pending_operator,
          NOT transitory failed.  Only genuine transitory failures (daemon down,
          unknown error) block version advancement.
        """
        existing = await self._proxy.call_list("list_consents")
        existing_caps = {c.get("capability", "") for c in existing}

        for spec in consents:
            if _is_high_risk_consent(spec.capability):
                logger.warning(
                    "hermes.config_sync.applier.high_risk_consent_skipped",
                    extra={"capability": spec.capability},
                )
                result.rejected.append(f"consent:pending_operator:{spec.capability}")
                continue
            if spec.capability in existing_caps:
                result.applied += 1
                continue
            resp, is_auth_failure = await self._call_grant_consent(
                spec.capability, spec.scope
            )
            if _is_ok_strict(resp):
                result.applied += 1
            elif is_auth_failure:
                logger.info(
                    "hermes.config_sync.applier.consent_pending_operator",
                    extra={"capability": spec.capability},
                )
                result.rejected.append(f"consent:pending_operator:{spec.capability}")
            else:
                result.failed.append(f"consent:{spec.capability}")

    async def _apply_egress(
        self, egress: EgressSpec, result: ApplyResult
    ) -> None:
        """Add domains to the allow-list.  Never removes; never changes mode.

        P0-3 domain validation: each domain must pass _DOMAIN_RE (same as the
        REST egress endpoint).  Wildcards, IPs, and whitespace are all rejected
        before the D-Bus call is made.
        """
        existing = await self._proxy.call_list("list_egress_grants")
        existing_domains = {g.get("domain", "") for g in existing}

        for domain in egress.allow_domains:
            normalised = domain.strip().lower().removeprefix("*.").rstrip(".")
            if not normalised or not _DOMAIN_RE.match(normalised):
                logger.warning(
                    "hermes.config_sync.applier.egress_invalid_domain",
                    extra={"domain": domain[:64]},
                )
                result.failed.append(f"egress:invalid:{domain[:64]}")
                continue
            if normalised in existing_domains:
                result.applied += 1
                continue
            resp = await self._call_mutator("add_egress_domain", normalised)
            # P2: strict check for egress.
            if _is_ok_strict(resp):
                result.applied += 1
            else:
                result.failed.append(f"egress:{normalised}")

    def _validate_license(self, license_spec: LicenseSpec) -> None:
        if not license_spec.plan:
            logger.warning("hermes.config_sync.applier.license_missing_plan")

    def _apply_directory(self, directory: DirectorySpec | None) -> None:
        """Replace-on-apply persistence of the Fase-3 department directory.

        None clears the stored directory — the roster/delegation UX fall
        back to today's local-roster-only behaviour (zero regression).

        Presentation-only data: a persistence failure here is logged but
        never marks the section failed (mirrors _validate_license) — it
        must never block version advancement or the critical security
        sections (agents/consents/egress) from applying.
        """
        if self._directory_store is None:
            return
        try:
            self._directory_store.update_directory(
                directory.model_dump() if directory is not None else None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.config_sync.applier.directory_persist_failed",
                extra={"reason": str(exc)},
            )

    # ------------------------------------------------------------------
    # Agent upsert helpers
    # ------------------------------------------------------------------

    async def _upsert_agent(
        self, spec: AgentSpec, cloud_managed: dict[str, dict], tenant_id: str
    ) -> bool:
        draft = _agent_draft(spec)
        if spec.agent_id in cloud_managed:
            # Guarded mutator (parity with create_agent): a raw proxy call here
            # would propagate a transient/AccessDenied error out of apply() and
            # abort the WHOLE sync with no rollback, stranding a half-applied
            # bundle and never advancing the version.
            resp = await self._call_mutator("update_agent", spec.agent_id, json.dumps(draft))
        else:
            draft["agent_id"] = spec.agent_id
            draft["managed_by"] = _CLOUD_MANAGED
            resp = await self._call_mutator("create_agent", json.dumps(draft))

        if not _is_ok_lenient(resp):
            return False

        created_id = (resp or {}).get("agent_id") or (resp or {}).get("id") or spec.agent_id
        await self._bind_non_mcp_capabilities(created_id, spec.capabilities)
        return await self._apply_access_scope(created_id, spec, tenant_id)

    async def _bind_non_mcp_capabilities(
        self, agent_id: str, capabilities: list[dict[str, str]]
    ) -> None:
        """Best-effort bind for non-MCP capabilities (skill, platform, ...).

        MCP-kind capabilities are deliberately SKIPPED here: BindCapabilityToAgent
        is D-Bus-denied for config-sync's uid by design (CWE-441 confused-deputy
        fix — org.hermes.Runtime1.conf line ~300). MCP authorization instead
        travels through the agent's AgentAccessScope (_apply_access_scope), which
        is the ALLOWED path and the one nous_engine._filter_mcp_skill reads.

        A binding failure here is logged but NEVER fails the agent's apply: the
        verb is structurally unreachable from this process, so treating it as
        fatal would block last_applied_version from ever advancing.
        """
        for cap in capabilities:
            if cap.get("kind") == _MCP_CAPABILITY_KIND:
                continue
            resp = await self._call_mutator(
                "bind_capability_to_agent",
                agent_id,
                cap.get("kind", "skill"),
                cap.get("id", ""),
                cap.get("version", ""),
            )
            if not _is_ok_lenient(resp):
                logger.info(
                    "hermes.config_sync.applier.capability_bind_best_effort_failed",
                    extra={"agent_id": agent_id, "kind": cap.get("kind"), "id": cap.get("id")},
                )

    async def _apply_access_scope(
        self, agent_id: str, spec: AgentSpec, tenant_id: str
    ) -> bool:
        """Land the bundle's AgentAccessScope, including bundle-authorized MCP
        servers (kind="mcp" capabilities) via the allowed set_agent_access_scope
        verb — the replacement for the forbidden BindCapabilityToAgent for MCP
        admission (see nous_engine._filter_mcp_skill).

        Reconcile is ADDITIVE ONLY when the bundle carries no access_scope for
        this agent: any existing local scope is left untouched (no
        clear_agent_access_scope call here). In that case, MCP-kind capabilities
        (if any) have nowhere allowed to land this sync — logged, never fatal.
        """
        mcp_servers = sorted(
            {
                cap.get("id", "")
                for cap in spec.capabilities
                if cap.get("kind") == _MCP_CAPABILITY_KIND and cap.get("id")
            }
        )
        if spec.access_scope is None:
            if mcp_servers:
                logger.warning(
                    "hermes.config_sync.applier.mcp_capabilities_without_access_scope",
                    extra={"agent_id": agent_id, "mcp_servers": mcp_servers},
                )
            return True

        scope = spec.access_scope.model_dump()
        scope["mcp_servers"] = mcp_servers
        resp = await self._call_mutator(
            "set_agent_access_scope", agent_id, json.dumps(scope), tenant_id
        )
        return _is_ok_lenient(resp)

    async def _delete_agent(self, agent_id: str) -> bool:
        try:
            return bool(await self._proxy.call_bool("delete_agent", agent_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.config_sync.applier.delete_agent_failed",
                extra={"agent_id": agent_id, "reason": str(exc)},
            )
            return False

    # ------------------------------------------------------------------
    # Proxy wrappers — enforce the verb allow-list (P0-3)
    # ------------------------------------------------------------------

    async def _fetch_agents(self) -> list[dict]:
        try:
            return await self._proxy.call_list("list_agents")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.config_sync.applier.fetch_agents_failed",
                extra={"reason": str(exc)},
            )
            return []

    async def _call_grant_consent(
        self, capability: str, scope: str
    ) -> tuple[dict, bool]:
        """Call grant_consent, distinguishing authorization failures from transitory ones.

        Returns (response_dict, is_auth_failure).

        is_auth_failure=True when the daemon rejected the call because the
        config_sync process uid is not in authorized_uids (operator-gating by
        design — not a bug, not a transitory failure).  Detected by inspecting
        the HTTP status on HTTPException (401) or the error text on AgentUnavailable
        and raw DBusError/PermissionError.

        is_auth_failure=False on genuine transitory failures (daemon down, network,
        parse error) — these remain in result.failed and block version advancement.
        """
        if "grant_consent" not in _ALLOWED_VERBS:
            return {"ok": False, "error": "verb_not_in_allowlist"}, False

        try:
            raw = await self._proxy.call_mutator("grant_consent", capability, scope)
            resp = raw if isinstance(raw, dict) else {"ok": bool(raw)}
            return resp, False
        except Exception as exc:  # noqa: BLE001
            is_auth = _is_authorization_error(exc)
            if is_auth:
                logger.info(
                    "hermes.config_sync.applier.consent_operator_gated",
                    extra={"capability": capability, "reason": type(exc).__name__},
                )
            else:
                logger.warning(
                    "hermes.config_sync.applier.consent_mutator_failed",
                    extra={"capability": capability, "reason": str(exc)},
                )
            return {"ok": False}, is_auth

    async def _call_mutator(self, verb: str, *args: Any) -> dict:
        """Call a D-Bus mutator guarded by the allow-list.  Never raises."""
        if verb not in _ALLOWED_VERBS:
            logger.error(
                "hermes.config_sync.applier.verb_not_allowed",
                extra={"verb": verb},
            )
            return {"ok": False, "error": "verb_not_in_allowlist"}

        try:
            result = await self._proxy.call_mutator(verb, *args)
            return result if isinstance(result, dict) else {"ok": bool(result)}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.config_sync.applier.mutator_failed verb=%s reason=%s",
                verb,
                str(exc),
                extra={"verb": verb, "reason": str(exc)},
            )
            return {"ok": False}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _cloud_managed_index(live_agents: list[dict]) -> dict[str, dict]:
    return {
        a["agent_id"]: a
        for a in live_agents
        if a.get("managed_by") == _CLOUD_MANAGED
    }


def _is_ok_lenient(result: dict | bool | None) -> bool:
    """Lenient check: None/{} treated as success for non-sensitive sections.

    Used for: providers, mcp, skills, agents, features.
    These sections have existing daemon verbs that may return {} on success.
    """
    if isinstance(result, bool):
        return result
    if result is None:
        return True
    if isinstance(result, dict):
        return result.get("ok", True) is not False
    return False


def _is_ok_strict(result: dict | bool | None) -> bool:
    """Strict check: requires explicit {"ok": True}.  None/{} → failure.

    P2: Used for sensitive sections — egress, consents, integrations.
    An empty or absent "ok" is not safe to assume success here.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return result.get("ok") is True
    return False


def _is_high_risk_consent(capability: str) -> bool:
    return any(capability.startswith(prefix) for prefix in _HIGH_RISK_CONSENT_PREFIXES)


def _is_authorization_error(exc: Exception) -> bool:
    """Return True when exc signals an operator-gating / authorization failure.

    Operator-gated = the daemon rejected the call because the config_sync
    service uid is not in authorized_uids for grant_consent.  This is permanent
    by design (the policy does not change between retries), so the consent should
    be classified as pending_operator, not as a transitory failure.

    Detection heuristic (no hard import of fastapi/dbus_fast to stay dependency-free):
    1. HTTPException with status_code == 401 (raised by _translate_dbus_error in
       DbusRuntimeProxy when it sees org.hermes.Error.Unauthorized).
    2. PermissionError (Python built-in — raised directly by the wiring authorize path
       in tests / in-process calls).
    3. Exception message contains auth/permission keywords from the D-Bus error name
       or from AgentUnavailable carrying the D-Bus error text.
    """
    # Case 1: fastapi HTTPException (no import — check via attribute).
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True

    # Case 2: Python PermissionError.
    if isinstance(exc, PermissionError):
        return True

    # Case 3: message-based heuristic (AgentUnavailable wrapping the D-Bus error,
    # or DbusAuthorizationError message).
    msg = str(exc).lower()
    return (
        "unauthorized" in msg
        or "not authorized" in msg
        or "rejected send message" in msg
        or "authz_denied" in msg
        or "no autorizado" in msg
    )


def _is_permanent_rejection(result: dict | bool | None) -> bool:
    """Return True when the daemon signals a permanent policy/security rejection.

    Criterion: {"ok": False, "blocked": True, ...} — the Security Center scan
    verdict FAIL (auto_block_fail=True) or a WARN without owner override.
    This shape is produced exclusively by install_hub_skill (and add_mcp_server)
    in DbusRuntimeServiceWiring._scan_hub_target / _scan_install_target.

    A permanent rejection NEVER becomes transitory on retry — the scan verdict
    is cached and the policy has not changed.  The applier records it in
    `result.rejected` so the sync version can still advance.

    Invariant: this function NEVER forces an install.  It only classifies
    whether a failure is worth retrying.
    """
    if not isinstance(result, dict):
        return False
    return result.get("ok") is False and result.get("blocked") is True


def _is_discretionary_warn_block(result: dict | bool | None) -> bool:
    """True when result is a Security-Center discretionary WARN block.

    Shape: {"ok": False, "blocked": True, "warn": True, "score": ..., ...} —
    produced EXCLUSIVELY by _scan_install_target's `if verdict == "WARN" and
    not allow_warn` branch (dbus_runtime_service.py). This is the only
    "warn": True shape the daemon emits: a genuine FAIL (ScanBlockedError
    path) or a scanner error carry no "warn" key, so this function never
    matches them — see _is_permanent_rejection for those.

    Used by _apply_one_mcp_server to gate the bundle-sourced auto-trust
    override to ONLY the discretionary WARN case: a hard FAIL/scanner-error
    block on a bundle MCP is never force-retried, it is rejected instead.
    """
    if not isinstance(result, dict):
        return False
    return (
        result.get("ok") is False
        and result.get("blocked") is True
        and result.get("warn") is True
    )


def _agent_draft(spec: AgentSpec) -> dict:
    return {
        "name": spec.name,
        "role": spec.role,
        "register": spec.register_tone,
        "primary_mission": spec.primary_mission,
        "instructions": spec.instructions,
        "color": spec.color,
        "language": spec.language,
        "golden_rules": spec.golden_rules,
        "forbidden_phrases": spec.forbidden_phrases,
        "autonomy_level": spec.autonomy_level,
        "department": spec.department,
        "provider_alias": spec.provider_alias,
    }


def _provider_draft(spec: ProviderSpec) -> dict:
    """Build the draft dict sent to add_provider / update_provider.

    api_key is included when the cloud bundle carries one.  The daemon
    stores it encrypted in the SecretsVault — identical to how a locally-
    configured key is stored.  NEVER log or expose the key here.
    """
    draft: dict = {
        "kind": spec.kind,
        "alias": spec.alias,
        "default_model": spec.default_model,
        "set_active": spec.set_active,
        # Stamp ownership so the daemon marks the row cloud-managed: the local
        # operator may not edit/delete it, and the applier can reconcile stale
        # cloud providers. Mirrors how cloud agents carry managed_by="cloud".
        "managed_by": "cloud",
    }
    if spec.base_url:
        draft["base_url"] = spec.base_url
    if spec.api_key:
        draft["api_key"] = spec.api_key
    return draft
