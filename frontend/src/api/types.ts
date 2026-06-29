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

// ── Roster ────────────────────────────────────────────────────────────────────

export interface RosterAgent {
  id: string
  name: string
  description: string
  source: 'ruflo' | 'custom'
  department: string
  is_default: boolean
  color: string | null
}

export interface RosterDepartment {
  id: string
  name: string
  kind: 'cerebro' | 'factory' | 'custom'
  agents: RosterAgent[]
}

export interface AgentRoster {
  departments: RosterDepartment[]
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
}

// ── Tasks ─────────────────────────────────────────────────────────────────────

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
  /** Always 'mfa' in the TOTP-only model */
  required_level?: string
  /** Whether the owner has enrolled a TOTP secret */
  mfa_enrolled?: boolean
  /** ISO-8601 creation timestamp. Used client-side to discard stale ghost cards. */
  created_at?: string | null
}

export interface MfaStatus {
  enrolled: boolean
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
  mfa_on_dangers?: boolean
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
  totp: string
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
  totp: string
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

// ── Agent stats ───────────────────────────────────────────────────────────────

export interface AgentStatToday {
  tokens: number
  cost_usd: number
  tasks: number
}

export interface AgentStat {
  agent_id: string
  name: string
  department: string
  color: string | null
  state: 'idle' | 'working'
  active_task_count: number
  today: AgentStatToday
  health: string | null
}

export interface AgentStatsResponse {
  available: boolean
  agents: AgentStat[]
}

// ── Training state ─────────────────────────────────────────────────────────────

export interface TrainingState {
  state: 'idle' | 'capturing' | 'paused' | 'review' | string
  session_id?: string
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
