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
  activity?: Array<{ agent_id: string; tool?: string }>
  ruflo_active?: boolean
}

export interface CreateAgentPayload {
  name: string
  role?: string
  primary_mission?: string
  department?: string
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
}

// ── Chat ──────────────────────────────────────────────────────────────────────

export interface ChatStartPayload {
  conversation_id?: string
  user_message: string
  dedup_key?: string
}

export interface ChatStartResponse {
  task_id: string
  stream_path?: string
}

export interface ConversationMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_call?: ToolCallDescriptor
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

export interface PendingApproval {
  proposal_id: string
  kind?: string
  summary: string
  target?: string
  parameters?: Record<string, unknown>
  /** task_id from the pre_tool_call hook; null for rows written before migration */
  conversation_id?: string | null
  /** MFA tier required to approve: 'mfa' | 'mfa_humanity' | 'mfa_riddle' */
  required_level?: string
  /** Whether the owner has enrolled a TOTP secret */
  mfa_enrolled?: boolean
  /** Whether the owner has configured a personal riddle */
  riddle_set?: boolean
}

export interface MfaStatus {
  enrolled: boolean
  riddle_set?: boolean
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
  riddle_answer: string | null
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

// Frames emitted by the WebSocket stream — discriminated by `kind`.
export type StreamFrame =
  | { kind: 'delta';          delta?: string; text?: string }
  | { kind: 'thinking_delta'; thinking?: string; delta?: string; text?: string }
  | { kind: 'tool_call';      tool_call?: ToolCallDescriptor; tool?: string; label?: string; target?: string }
  | { kind: 'status';         message?: string; status?: string }
  | { kind: 'done' }
  | { kind: 'error';          message?: string }
