// Domain types matching the shapes returned by /api/v1/* endpoints.
// Source of truth: src/hermes/shell_server/cowork/agents_api.py (AgentDraft)
// and the vanilla js/api.js call shapes.

export interface Agent {
  id: string
  name: string
  role: string
  primary_mission: string
  instructions: string
  language: string
  color: string
  golden_rules: string[]
  autonomy_level: string
  is_default: boolean
}

export interface ActiveAgentResponse {
  active_agent_id: string
}

export interface RuntimeStatus {
  state: string
  active_task_count: number
  active_agent_id?: string
  activity?: Array<{ task_id?: string; agent_id: string; tool?: string }>
  ruflo_active?: boolean
  /**
   * HONEST source of truth for the "usando el navegador / En vivo" chip: true iff
   * the jailed browser currently has a REAL (non-blank) page open. NOT derived from
   * tool names — a failed browser_navigate or a web_search never sets it.
   */
  browser_live?: boolean
  /**
   * Recent, still-live delegation edges (orchestrator → specialist), emitted by
   * the backend at the moment a `delegate_task` fires and kept for a short TTL.
   * Drives the real Cerebro→especialista flow in the swarm view — an actual
   * event, never fabricated. `from`/`to` are roster agent ids.
   */
  delegations?: Array<{ from: string; to: string; task_id?: string; label?: string; since?: string }>
}

export interface CreateAgentPayload {
  name: string
  role?: string
  primary_mission?: string
  department?: string
}

export interface UpdateAgentPayload {
  name?: string
  role?: string
  primary_mission?: string
  department?: string
  instructions?: string
  language?: string
  autonomy_level?: string
}

export interface UpdateTaskPayload {
  label?: string
  cron?: string
  instruction?: string
  target_agent_id?: string
  risk_ceiling?: string
  one_shot?: boolean
  enabled?: boolean
}

// ── Workspace files ───────────────────────────────────────────────────────────

export interface WorkspaceFile {
  name: string
  path: string
  size: number
  /** Whether the entry is a directory (new folder-browser API) */
  is_dir?: boolean
  /** Human-readable kind: 'directory', 'text', 'code', 'image', 'spreadsheet', etc. */
  kind?: string
  /** ISO-8601 modification timestamp */
  modified?: string
}

// ── Chat ──────────────────────────────────────────────────────────────────────

export interface ChatStartPayload {
  conversation_id?: string
  user_message: string
  dedup_key?: string
  /** Bind this conversation to a specific agent. Omit (or use "default") for the CEO agent. */
  agent_id?: string
}

export interface ChatStartResponse {
  task_id: string
  stream_path?: string
}

export interface ConversationMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_call?: ToolCallDescriptor
  /** task_id of the backend task that produced this assistant turn; null for user messages */
  task_id?: string | null
  /** 'streaming' = partial (turn in-flight, persisted incrementally) | 'complete' | null */
  status?: string | null
}

export interface ConversationDetail {
  id: string
  title?: string
  messages: ConversationMessage[]
}

export interface ConversationSummary {
  id: string
  title?: string
  created_at?: string
  updated_at?: string
}

export interface ToolCallDescriptor {
  tool?: string
  tool_name?: string
  label?: string
  target?: string
}

// ── Providers ─────────────────────────────────────────────────────────────────

export interface Provider {
  provider_id: string
  alias?: string
  name?: string
  kind?: string
  category?: string
  auth_type?: string
  default_model?: string
  base_url?: string
  is_active?: boolean
  supports_oauth?: boolean
  /** "cloud" → set by the org's Enterprise policy; read-only for the operator. */
  managed_by?: string | null
}

// ── Skills ────────────────────────────────────────────────────────────────────

export interface Skill {
  package_id?: string
  skill_id?: string
  skill_name?: string
  name?: string
  slug?: string
  state?: string
  version?: string
  surface_kinds?: string | string[]
}

export interface HubSkillResult {
  identifier?: string
  slug?: string
  name?: string
  skill_name?: string  // listHubSkills may return installed items with this field
  description?: string
  trust_level?: string
  source?: string
  repo?: string
  url?: string
  homepage?: string
}

export interface HubInstallResponse {
  op_id?: string
  status?: string
  ok?: boolean
  blocked?: boolean
  score?: number
  risks?: string[]
  scan_id?: string
  error?: string
}

export interface HubOpStatus {
  status?: string
  error?: string
  message?: string
}

// ── Integrations (Composio) ───────────────────────────────────────────────────

export interface ComposioStatus {
  has_key: boolean
  enabled?: boolean
  entity_id?: string
}

export interface ComposioApp {
  slug: string
  name?: string
  description?: string
  logo?: string
}

export interface WebSearchStatus {
  brave?: boolean
  ddgs_fallback?: boolean
}

// ── MCP ───────────────────────────────────────────────────────────────────────

export interface McpServer {
  server_id?: string
  id?: string
  slug?: string    // registry / ruflo entries may use slug as the identifier
  name?: string
  label?: string
  argv?: string | string[]
  health?: string
  tool_count?: number
  // Seeded companion (024) only — "esperando_servicio" | "listo" | future
  // finer states from the companion's own /mcp/health. Absent for every
  // other server (owner-typed managed-remote URL, user-added, built-in).
  companion_status?: string
}

export interface McpRegistryEntry {
  server_id?: string
  id?: string
  name?: string
  label?: string
  description?: string
  argv?: string | string[]
  runner?: string
  repository?: string
  homepage?: string
  website?: string
  tag?: string
  installable?: boolean
  unsupported_reason?: string
  env_vars?: Array<string | { key: string; label?: string; required?: boolean; secret?: boolean }>
}

export interface McpAddResponse {
  server_id?: string
  tool_count?: number
  ok?: boolean
  error?: string
  blocked?: boolean
  scan_id?: string
  verdict?: string
  risks?: unknown[]
}

export interface ManagedRemoteEndpointsResponse {
  endpoints: Record<string, string>
}

// ── Tasks ─────────────────────────────────────────────────────────────────────

/** Read-model contract; until the daemon supports it the UI reports unavailable. */
export interface TaskDashboardItem {
  task_id: string
  label: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'pending_approval' | 'rejected' | 'cancelled'
  source: 'local' | 'enterprise'
  requested_by?: string | null
  created_at?: string | null
  updated_at?: string | null
  conversation_id?: string | null
  result?: string | null
  approval_ids?: string[]
  enterprise_sync?: { state: 'pending' } | { state: 'blocked'; reason: string }
}

export interface TaskDashboardResponse {
  available: boolean
  tasks: TaskDashboardItem[]
  has_more: boolean
}

export interface ConfiguredTask {
  trigger_id?: string
  task_id?: string
  id?: string
  label?: string
  title?: string
  name?: string
  cron?: string
  schedule?: string
  recurrence?: string
  recurrence_human?: string
  trigger?: { cron?: string }
  instruction?: string
  enabled?: boolean
  one_shot?: boolean
  last_status?: string
  next_run_at?: string
  target_agent_id?: string
  agent_id?: string
  risk_ceiling?: string
}

export interface RecentTask {
  task_id?: string
  label?: string
  name?: string
  status?: string
  claimed_at?: string
  enqueued_at?: string
  started_at?: string
}

export interface ConfiguredTasksResponse {
  available?: boolean
  tasks?: ConfiguredTask[]
}

export interface RecentTasksResponse {
  available?: boolean
  tasks?: RecentTask[]
}

export interface CreateTaskPayload {
  label: string
  cron: string
  instruction: string
  target_agent_id?: string
  risk_ceiling?: string
  one_shot?: boolean
}

// ── Security ──────────────────────────────────────────────────────────────────

export interface SecurityScan {
  scan_id?: string
  id?: string
  name?: string
  identifier?: string
  target?: string
  kind?: string
  verdict?: string
  severity?: string
  score?: number
  decision?: string
}

export interface AuditHead {
  hash?: string
  head?: string
  timestamp?: string
}

export interface EgressDomainsResponse {
  domains: string[]
}

export type EgressMode = 'allow' | 'deny'

export interface EgressModeResponse {
  mode: EgressMode
  /** allow-list (used when mode === 'deny') */
  domains: string[]
  /** manual block-list (used when mode === 'allow') */
  deny: string[]
  /** count of threat-intelligence blocked domains active in the system */
  blocklist_count?: number
}

// ── Governed tailnet (spec 022) ─────────────────────────────────────────────

export interface TailnetPeer {
  name: string
  online: boolean
}

export interface TailnetLastAttempt {
  at: string
  ok: boolean
  error_kind: string | null
}

export interface TailnetStatus {
  // 025 hallazgo D: `configured` means LOGGED IN (== online) — see
  // tailnet/api.py's _read_status. Use last_attempt to distinguish
  // "never tried" from "pending" from "the key was rejected".
  configured: boolean
  online: boolean
  node_name: string | null
  magicdns_suffix: string | null
  tailnet: string | null
  peers: TailnetPeer[]
  last_attempt: TailnetLastAttempt | null
}

// ── Governed SSH allow-list (spec 022 v2) ───────────────────────────────────

export interface SshHostEntry {
  host: string
  approved_at: string | null
}

export interface SshHostsResponse {
  hosts: SshHostEntry[]
}

/** Emergency brake — freno de emergencia. Engaging needs no MFA; releasing does. */
export interface KillSwitchStatus {
  engaged: boolean
  reason: string | null
  changed_by: string | null
  changed_at: string | null
}

export interface PendingApproval {
  proposal_id: string
  kind?: string
  summary: string
  target?: string
  parameters?: Record<string, unknown>
  /** Raw technical description for the "Ver detalles técnicos" disclosure panel. */
  technical_detail?: string
  /** task_id from the pre_tool_call hook; null for rows written before migration */
  conversation_id?: string | null
  /** Server-classified verification level; never inferred from a UI setting. */
  required_level?: string
  /** Enterprise-routed requests cannot be approved locally. Denial is allowed. */
  route?: 'local' | 'enterprise'
  /** Whether the owner has enrolled a TOTP secret */
  mfa_enrolled?: boolean
  /** ISO-8601 creation timestamp. Used client-side to discard stale ghost cards. */
  created_at?: string | null
}

/**
 * Inbound cross-human delegation card (FASE 3 A2A) — a colleague's assistant
 * asking THIS owner's agent to pick up work. Mirrors the exact shape returned
 * by GET /api/v1/inbound-delegations (DelegationApprovalService.list_pending):
 * message_id, from_employee_id, body, issued_at, created_at only — no
 * from_agent_id/correlation_id (those stay server-side, CTRL-P1-5: no
 * secrets/signature reach the web surface).
 */
export interface InboundDelegation {
  message_id: string
  from_employee_id: string
  body: string
  issued_at: string
  created_at: string
}

export interface PolicyCatalogEntry {
  name: string
  label: string
  category: string
  delicacy: 'normal' | 'delicate' | 'most_delicate'
  enabled: boolean
  llm_visible: boolean
  origin: 'native' | 'capability' | 'mcp' | 'composio'
}

export interface PoliciesResponse {
  preset?: string
  tools?: Record<string, boolean>
  approval_on_dangers?: boolean
  catalog?: PolicyCatalogEntry[]
}

export interface InstallDecisionPayload {
  scan_id: string
  decision: 'allow'
  identifier: string
  kind: string
  score: number
  verdict: string
  risks_json: string
}

// ── Notifications ─────────────────────────────────────────────────────────────

export type NotificationKind = 'task' | 'chat' | 'system'
export type NotificationStatus = 'ok' | 'error' | 'info'

export interface Notification {
  id: string
  kind: NotificationKind
  title: string
  body: string
  status: NotificationStatus
  conversation_id: string | null
  created_at: string
  read: boolean
}

export interface UnreadCountResponse {
  count: number
}

// ── Security install scan ──────────────────────────────────────────────────────

export interface InstallRisk {
  category: string
  severity: string
  message: string
  evidence_ref?: string
}

export interface InstallScanResponse {
  scan_id: string
  verdict: 'PASS' | 'WARN' | 'FAIL'
  score: number
  engine: string
  engine_label: string
  requires_owner_approval: boolean
  risks: InstallRisk[]
  identifier?: string
  kind?: string
}

export interface SecurityDecisionPayload {
  scan_id: string
  decision: 'approve'
  identifier: string
  kind: string
  score: number
  verdict: string
  risks_json: string
  mcp_approval?:
    | { operation: 'add'; server_id: string; label?: string; argv: string[]; env: Record<string, string> }
    | { operation: 'managed_remote'; slug: string; url: string }
}

export interface SecurityDecisionResponse {
  ok?: boolean
  error?: string
  // One-use owner confirmation (≤120s), bound to this session/identifier/action.
  // Issued only after a recorded decision. Community does not use MFA.
  approval_grant?: string
}

// ── Skill details ──────────────────────────────────────────────────────────────

export interface SkillDetails {
  package_id: string
  skill_id?: string
  skill_name?: string
  version?: string
  state?: string
  surface_kinds?: string | string[]
  skill_kind?: string
  instructions: string | null
  instructions_path?: string
  created_at?: string
}

// ── Usage / Cost ──────────────────────────────────────────────────────────────

export type UsagePeriod = '7d' | '30d' | 'mtd'
export type UsageDimension = 'cost' | 'tokens'

export interface UsageTopModel {
  model: string
  cost_usd: number
  share: number
}

export interface UsageSummary {
  available: boolean
  period: string
  currency: string
  total_cost_usd: number
  projected_cost_usd: number
  total_tokens: number
  cycles: number
  failures: number
  self_hosted_cycles: number
  top_models: UsageTopModel[]
}

export interface UsageAgent {
  agent_id: string
  name: string
  department: string
  cost_usd: number
  total_tokens: number
  cycles: number
  share: number
}

export interface UsageByAgent {
  available: boolean
  agents: UsageAgent[]
}

export interface UsageTimeseriesPoint {
  day: string
  cost_usd: number
  tokens: number
  cycles: number
}

export interface UsageTimeseries {
  available: boolean
  points: UsageTimeseriesPoint[]
}

export interface ConversationUsageCycle {
  ts: string
  model: string
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number
  tool_calls: number
  latency_ms: number
  outcome: string
}

export interface ConversationUsage {
  conversation_id: string
  cost_usd: number
  total_tokens: number
  cycles: ConversationUsageCycle[]
}

// ── Memory ────────────────────────────────────────────────────────────────────

export interface MemoryItem {
  id?: string
  /** Primary display field returned by the backend. */
  content_truncated?: string
  content?: string
  text?: string
  target?: string
  entry_index?: number
  created_at?: string
  [key: string]: unknown
}

export interface MemoryEntryDetail {
  id: string
  target: string
  content: string
  entry_index: number
}

// ── Ads bridge (026, contracts/sso.md) ──────────────────────────────────────

// Mirrors the coarse states `CompanionHealthChecker` (T004) derives from the
// companion's real /mcp/health payload — never a fabricated "ready" (FR-009).
export type AdsAvailabilityReason =
  | 'not_installed'
  | 'unreachable'
  | 'unauthorized'
  | 'no_accounts'

export interface AdsBridgeSessionResponse {
  status: 'ready' | 'unavailable'
  reason: AdsAvailabilityReason | null
}

// Frames emitted by the WebSocket stream — discriminated by `kind`.
// `seq` is a monotonically increasing integer per task_id, added to every frame
// so the client can deduplicate replay on reconnect (discard seq <= lastSeq).
export type StreamFrame =
  | { kind: 'delta';          delta?: string; text?: string; seq?: number }
  | { kind: 'thinking_delta'; thinking?: string; delta?: string; text?: string; seq?: number }
  | { kind: 'tool_call';      tool_call?: ToolCallDescriptor; tool?: string; label?: string; target?: string; seq?: number }
  | { kind: 'status';         message?: string; status?: string; seq?: number }
  | { kind: 'done';           seq?: number }
  | { kind: 'error';          message?: string; seq?: number }

// ── Install requests (028/029, contracts/install-request.md) ───────────────────
//
// The sandbox never creates sibling containers: the UI leaves a request marker
// under /var/lib/hermes/instance/ and the HOST agent (safent agent, or the app
// itself when open) claims and fulfils it. Closed vocabulary by design — no
// field carries a command, path, URL or argument (contract §1 invariant 1).

export type HostVerb =
  | 'install_companion'
  | 'repair_companion'
  | 'remove_companion'
  | 'update_system'
  | 'uninstall_system'

export type InstallRequestState = 'pending' | 'claimed' | 'applied' | 'expired' | 'failed'

export interface InstallRequestProgress {
  done: number
  total?: number
  unit: 'bytes' | 'layers' | 'steps'
}

export interface InstallRequestFailure {
  code: string
  /** Owner-facing sentence, already in Spanish — the frontend renders it as-is. */
  label: string
  retryable: boolean
}

export interface InstallRequestStatus {
  verb: HostVerb
  state: InstallRequestState
  /** Echoes the live engine stage (contracts/app-engine.md §3 StageId). */
  stage?: string
  progress?: InstallRequestProgress
  expires_at: string
  last_failure?: InstallRequestFailure
}

export interface InstallRequestResponse {
  accepted: boolean
  request?: InstallRequestStatus
  code?: 'unknown_verb' | 'unknown_slug'
}

export interface InstallRequestsListResponse {
  requests: InstallRequestStatus[]
}

// ── System update (028, contracts/update.md) ───────────────────────────────────

export interface VersionSet {
  app: string
  engine: string
  companion: string | null
}

export type UpdatePieceKind = 'app' | 'engine' | 'companion'

export interface UpdatePiece {
  kind: UpdatePieceKind
  size_bytes?: number
}

/** The rich shape the Tauri host shell injects once it has checked for real (contract §3). */
export interface SafentUpdateGlobal {
  available: boolean
  current: VersionSet
  to?: VersionSet
  pieces?: UpdatePiece[]
  checked_at: string
}
