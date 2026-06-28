import { token, refreshToken } from '../lib/token'
import type {
  Agent,
  ActiveAgentResponse,
  RuntimeStatus,
  ChatStartPayload,
  ChatStartResponse,
  ConversationDetail,
  ConversationSummary,
  StreamFrame,
  CreateAgentPayload,
  UpdateAgentPayload,
  UpdateTaskPayload,
  Provider,
  Skill,
  HubSkillResult,
  HubInstallResponse,
  HubOpStatus,
  ComposioStatus,
  ComposioApp,
  WebSearchStatus,
  McpServer,
  McpRegistryEntry,
  McpAddResponse,
  ConfiguredTasksResponse,
  RecentTasksResponse,
  CreateTaskPayload,
  ConfiguredTask,
  SecurityScan,
  AuditHead,
  EgressDomainsResponse,
  EgressMode,
  EgressModeResponse,
  PendingApproval,
  MfaStatus,
  PoliciesResponse,
  InstallDecisionPayload,
  AgentRoster,
  WorkspaceFile,
  MemoryItem,
  MemoryEntryDetail,
  Notification,
  UnreadCountResponse,
  InstallScanResponse,
  SecurityDecisionPayload,
  SkillDetails,
  TrainingState,
  UsageSummary,
  UsageByAgent,
  UsageTimeseries,
  ConversationUsage,
  UsagePeriod,
  UsageDimension,
  AgentStatsResponse,
} from './types'

// Mirrors the timeout strategy in vanilla api.js: snappy GETs fail fast;
// long-running mutations get explicit larger timeouts.
const DEFAULT_TIMEOUT_MS = 20_000
const BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(message: string, status: number, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

interface RequestOptions extends RequestInit {
  timeoutMs?: number
}

async function request<T>(path: string, options: RequestOptions = {}, _retried = false): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, headers: extraHeaders, ...rest } = options

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(extraHeaders as Record<string, string> ?? {}),
  }

  const tok = token()
  if (tok && !headers['Authorization']) {
    headers['Authorization'] = `Bearer ${tok}`
  }

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)

  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, { ...rest, headers, signal: ctrl.signal })
  } catch (err) {
    clearTimeout(timer)
    const e = err as Error
    if (e.name === 'AbortError') {
      throw new ApiError(
        `La petición tardó demasiado (${Math.round(timeoutMs / 1000)}s) y se canceló.`,
        0,
        null,
      )
    }
    throw new ApiError(`Error de red: ${e.message}`, 0, null)
  }
  clearTimeout(timer)

  // Session token rotated/expired mid-use → renew once and retry, so the user
  // never hits a dead 401 while the tab is active.
  if (res.status === 401 && !_retried && token() && path !== '/session/refresh') {
    if (await refreshToken()) {
      return request<T>(path, options, true)
    }
  }

  if (!res.ok) {
    let body: unknown = null
    try { body = await res.json() } catch { /* non-JSON */ }
    const b = body as Record<string, unknown> | null
    const message =
      (b?.detail as Record<string, unknown> | undefined)?.message as string
      ?? b?.detail as string
      ?? `HTTP ${res.status}`
    throw new ApiError(message, res.status, body)
  }

  if (res.status === 204) return null as T

  const json = await res.json() as Record<string, unknown>

  // Mirror the vanilla api.js {ok:false} guard (mutators return 2xx with ok:false
  // on daemon-level failures — e.g. addMcpServer).
  if (json['ok'] === false) {
    throw new ApiError(
      (json['error'] as string | undefined) ?? 'La operación falló.',
      res.status,
      json,
    )
  }

  return json as T
}

// ── Agents ────────────────────────────────────────────────────────────────────

export function listAgents(): Promise<Agent[]> {
  return request<Agent[]>('/agents').catch(() => [])
}

export function getActiveAgent(): Promise<ActiveAgentResponse> {
  return request<ActiveAgentResponse>('/agents/active').catch(
    () => ({ active_agent_id: '' }),
  )
}

export function setActiveAgent(agentId: string): Promise<unknown> {
  return request<unknown>(`/agents/${encodeURIComponent(agentId)}/activate`, { method: 'POST' })
}

export function createAgent(payload: CreateAgentPayload): Promise<Agent> {
  return request<Agent>('/agents', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateAgent(agentId: string, payload: UpdateAgentPayload): Promise<Agent> {
  return request<Agent>(`/agents/${encodeURIComponent(agentId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteAgent(agentId: string): Promise<unknown> {
  return request<unknown>(`/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' })
}

export function getAgentRoster(): Promise<AgentRoster> {
  return request<AgentRoster>('/agents/roster').catch(
    () => ({ departments: [] }),
  )
}

export function getDefaultRoster(): Promise<{ enabled: boolean }> {
  return request<{ enabled: boolean }>('/agents/default-roster').catch(() => ({ enabled: true }))
}

export function setDefaultRoster(enabled: boolean): Promise<{ enabled: boolean }> {
  return request<{ enabled: boolean }>('/agents/default-roster', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })
}

/**
 * Upload a file to the workspace. Uses fetch directly (not `request`) because
 * `request` forces Content-Type: application/json; multipart boundary must be
 * set by the browser automatically when we pass a FormData body.
 */
export async function uploadWorkspaceFile(file: File): Promise<WorkspaceFile> {
  const tok = token()
  const body = new FormData()
  body.append('file', file)

  const headers: Record<string, string> = {}
  if (tok) headers['Authorization'] = `Bearer ${tok}`

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 60_000)

  let res: Response
  try {
    res = await fetch('/api/v1/workspace/files', {
      method: 'POST',
      headers,
      body,
      signal: ctrl.signal,
    })
  } catch (err) {
    clearTimeout(timer)
    const e = err as Error
    if (e.name === 'AbortError') throw new ApiError('La subida tardó demasiado.', 0, null)
    throw new ApiError(`Error de red: ${e.message}`, 0, null)
  }
  clearTimeout(timer)

  if (!res.ok) {
    let body2: unknown = null
    try { body2 = await res.json() } catch { /* non-JSON */ }
    const b = body2 as Record<string, unknown> | null
    const message = b?.detail as string ?? `HTTP ${res.status}`
    throw new ApiError(message, res.status, body2)
  }

  return res.json() as Promise<WorkspaceFile>
}

// ── Providers ─────────────────────────────────────────────────────────────────

export function listProviders(): Promise<Provider[]> {
  return request<Provider[]>('/providers')
}

export function listNativeProviders(): Promise<Provider[]> {
  return request<Provider[]>('/providers/native')
}

export function addProvider(payload: Record<string, unknown>): Promise<Provider> {
  return request<Provider>('/providers', { method: 'POST', body: JSON.stringify(payload) })
}

/**
 * Configure a NATIVE catalogue provider (OpenAI, Anthropic, …) by kind + api_key.
 * The native catalogue path must NOT use addProvider() → POST /providers, which
 * requires `default_model` and rejects `provider_id` (422). The daemon resolves
 * the default model for a native kind itself.
 */
export function configureNativeProvider(payload: {
  kind: string
  api_key: string
  set_active?: boolean
}): Promise<Provider> {
  return request<Provider>('/providers/native', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function setActiveProvider(providerId: string): Promise<unknown> {
  return request<unknown>(`/providers/${encodeURIComponent(providerId)}/activate`, { method: 'POST' })
}

export function testProvider(providerId: string): Promise<{ ok?: boolean }> {
  return request<{ ok?: boolean }>(
    `/providers/${encodeURIComponent(providerId)}/test`,
    { method: 'POST', timeoutMs: 60_000 },
  )
}

export function deleteProvider(providerId: string): Promise<unknown> {
  return request<unknown>(`/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' })
}

export function startProviderOAuth(providerId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `/providers/${encodeURIComponent(providerId)}/oauth/start`,
    { method: 'POST' },
  )
}

export function getProviderOAuthStatus(sessionId: string): Promise<{ status?: string; error?: string; error_message?: string }> {
  return request<{ status?: string; error?: string; error_message?: string }>(
    `/providers/oauth/${encodeURIComponent(sessionId)}`,
  ).catch(() => ({ status: 'unknown' }))
}

// ── Skills ────────────────────────────────────────────────────────────────────

export function listSkills(): Promise<Skill[]> {
  return request<Skill[]>('/skills')
}

export function searchSkillsHub(query: string): Promise<{ results?: HubSkillResult[] } | HubSkillResult[]> {
  return request<{ results?: HubSkillResult[] } | HubSkillResult[]>(
    `/skills/hub/search?q=${encodeURIComponent(query)}`,
  ).catch(() => [])
}

export function listHubSkills(): Promise<HubSkillResult[]> {
  return request<HubSkillResult[]>('/skills/hub').catch(() => [])
}

export function installSkill(identifier: string, force = false): Promise<HubInstallResponse> {
  return request<HubInstallResponse>('/skills/hub/install', {
    method: 'POST',
    body: JSON.stringify({ identifier, force }),
  })
}

export function getHubOpStatus(opId: string): Promise<HubOpStatus> {
  return request<HubOpStatus>(`/skills/hub/ops/${encodeURIComponent(opId)}`).catch(
    () => ({ status: 'unknown' }),
  )
}

export function uninstallHubSkill(name: string): Promise<HubInstallResponse> {
  return request<HubInstallResponse>(`/skills/hub/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export function promoteSkill(packageId: string): Promise<unknown> {
  return request<unknown>(`/skills/${encodeURIComponent(packageId)}/promote`, {
    method: 'POST',
    body: JSON.stringify({ confirm: true }),
  })
}

export function createTrainingSession(payload: { skill_name: string; description: string; surface_kind: string }): Promise<{ session_id: string }> {
  return request<{ session_id: string }>('/training', { method: 'POST', body: JSON.stringify(payload) })
}

export function startTrainingRecording(sessionId: string): Promise<unknown> {
  return request<unknown>(`/training/${encodeURIComponent(sessionId)}/start`, { method: 'POST', body: '{}' })
}

export function stopTrainingRecording(sessionId: string): Promise<unknown> {
  return request<unknown>(`/training/${encodeURIComponent(sessionId)}/stop`, { method: 'POST', body: '{}' })
}

export function synthesizeSkill(sessionId: string): Promise<unknown> {
  return request<unknown>(`/training/${encodeURIComponent(sessionId)}/synthesize`, { method: 'POST', body: '{}' })
}

export function abandonTrainingSession(sessionId: string): Promise<unknown> {
  return request<unknown>(
    `/training/${encodeURIComponent(sessionId)}/abandon`,
    { method: 'POST', body: '{}' },
  ).catch(() => ({}))
}

export function pauseTrainingRecording(sessionId: string): Promise<TrainingState> {
  return request<TrainingState>(`/training/${encodeURIComponent(sessionId)}/pause`, { method: 'POST', body: '{}' })
}

export function resumeTrainingRecording(sessionId: string): Promise<TrainingState> {
  return request<TrainingState>(`/training/${encodeURIComponent(sessionId)}/resume`, { method: 'POST', body: '{}' })
}

export function cancelTrainingRecording(sessionId: string): Promise<TrainingState> {
  return request<TrainingState>(`/training/${encodeURIComponent(sessionId)}/cancel`, { method: 'POST', body: '{}' })
}

export function getSkillDetails(packageId: string): Promise<SkillDetails> {
  return request<SkillDetails>(`/skills/${encodeURIComponent(packageId)}/details`)
}

// ── Integrations (Composio) ───────────────────────────────────────────────────

export function getComposioStatus(): Promise<ComposioStatus> {
  return request<ComposioStatus>('/integrations/composio/status')
}

export function listComposioConnected(): Promise<ComposioApp[]> {
  return request<ComposioApp[]>('/integrations/composio/connected')
}

export function listComposioApps(): Promise<ComposioApp[]> {
  return request<ComposioApp[]>('/integrations/composio/toolkits')
}

export function connectComposioApp(slug: string): Promise<{ redirect_url?: string }> {
  return request<{ redirect_url?: string }>('/integrations/composio/connect', {
    method: 'POST',
    body: JSON.stringify({ toolkit_slug: slug }),
  })
}

export function setComposioApiKey(apiKey: string): Promise<unknown> {
  return request<unknown>('/integrations/composio/key', {
    method: 'POST',
    body: JSON.stringify({ api_key: apiKey }),
  })
}

export function disconnectComposioApp(slug: string): Promise<unknown> {
  return request<unknown>(`/integrations/composio/connected/${encodeURIComponent(slug)}`, {
    method: 'DELETE',
  })
}

export function getWebSearchStatus(): Promise<WebSearchStatus> {
  return request<WebSearchStatus>('/web-search/status')
}

export function setWebSearchKey(provider: string, apiKey: string): Promise<{ ok?: boolean; error?: string }> {
  return request<{ ok?: boolean; error?: string }>('/web-search/key', {
    method: 'POST',
    body: JSON.stringify({ provider, api_key: apiKey }),
  })
}

// ── MCP ───────────────────────────────────────────────────────────────────────

export function listMcpServers(): Promise<McpServer[]> {
  return request<McpServer[]>('/mcp')
}

export function addMcpServer(payload: Record<string, unknown>): Promise<McpAddResponse> {
  // The daemon connects eagerly and reports failures as {ok:false} with 2xx.
  // The request<T> helper already throws ApiError on ok:false, but addMcpServer
  // also does a dedicated tool_count=0 warning, so we return raw and let callers
  // surface that separately.
  return request<McpAddResponse>('/mcp', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeoutMs: 300_000,
  })
}

export function removeMcpServer(serverId: string): Promise<unknown> {
  return request<unknown>(`/mcp/${encodeURIComponent(serverId)}`, { method: 'DELETE' })
}

export function searchMcpRegistry(query: string, limit = 30): Promise<McpRegistryEntry[]> {
  return request<McpRegistryEntry[]>(
    `/mcp/registry?q=${encodeURIComponent(query)}&limit=${limit}`,
    { timeoutMs: 25_000 },
  )
}

// ── Tasks ─────────────────────────────────────────────────────────────────────

export function listConfiguredTasks(): Promise<ConfiguredTasksResponse> {
  return request<ConfiguredTasksResponse>('/tasks/configured').catch(
    () => ({ available: false, tasks: [] }),
  )
}

export function listRecentTasks(limit = 20): Promise<RecentTasksResponse> {
  return request<RecentTasksResponse>(`/tasks/recent?limit=${limit}`).catch(
    () => ({ available: false, tasks: [] }),
  )
}

export function createTask(payload: CreateTaskPayload): Promise<ConfiguredTask> {
  return request<ConfiguredTask>('/tasks/scheduled', { method: 'POST', body: JSON.stringify(payload) })
}

export function getTask(taskId: string): Promise<ConfiguredTask> {
  return request<ConfiguredTask>(`/tasks/scheduled/${encodeURIComponent(taskId)}`)
}

export function updateTask(taskId: string, payload: UpdateTaskPayload): Promise<ConfiguredTask> {
  return request<ConfiguredTask>(`/tasks/scheduled/${encodeURIComponent(taskId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function deleteTask(taskId: string): Promise<unknown> {
  return request<unknown>(`/tasks/scheduled/${encodeURIComponent(taskId)}`, { method: 'DELETE' })
}

export function toggleTask(taskId: string, enabled: boolean): Promise<unknown> {
  return request<unknown>(`/tasks/scheduled/${encodeURIComponent(taskId)}/enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })
}

// ── Runtime ───────────────────────────────────────────────────────────────────

export function getRuntimeStatus(): Promise<RuntimeStatus> {
  return request<RuntimeStatus>('/runtime/status').catch(
    () => ({ state: 'unknown', active_task_count: 0 }),
  )
}

/**
 * Per-agent live stats: state (idle/working), today's task count, cost, tokens.
 * Falls back to an empty-but-valid shape so callers can guard with `?? []` on agents.
 */
export function getAgentStats(): Promise<AgentStatsResponse> {
  return request<AgentStatsResponse>('/runtime/agent-stats').catch(
    () => ({ available: false, agents: [] }),
  )
}

// ── Chat ──────────────────────────────────────────────────────────────────────

/**
 * Enqueue a chat message. Returns { task_id, stream_path }.
 * Mirrors vanilla: request('/chat', { method: 'POST', body: ... })
 */
export function postChat(payload: ChatStartPayload): Promise<ChatStartResponse> {
  // Enqueue only — returns a task_id in ~20ms. The stream (seconds/hours)
  // flows over WebSocket. No timeout override needed: the POST is fast.
  return request<ChatStartResponse>('/chat', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** Get conversation detail (messages). */
export function getConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/chat/conversations/${encodeURIComponent(id)}`)
}

/** List conversation summaries. */
export function listConversations(agentId?: string): Promise<ConversationSummary[]> {
  const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ''
  return request<ConversationSummary[]>(`/chat/conversations${qs}`).catch(() => [])
}

// ── Security ──────────────────────────────────────────────────────────────────

export function getSecurityScans(): Promise<SecurityScan[]> {
  return request<SecurityScan[]>('/security/scans').catch(() => [])
}

export function getAuditChainHead(): Promise<AuditHead | null> {
  return request<AuditHead>('/security/audit/head').catch(() => null)
}

export function getSecurityPolicy(): Promise<unknown> {
  return request<unknown>('/security/policy').catch(() => null)
}

export function recordInstallDecision(payload: InstallDecisionPayload): Promise<unknown> {
  return request<unknown>('/security/decisions', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeoutMs: 30_000,
  })
}

export function scanInstall(kind: 'mcp' | 'skill', identifier: string): Promise<InstallScanResponse> {
  return request<InstallScanResponse>('/security/scans/install', {
    method: 'POST',
    body: JSON.stringify({ kind, identifier }),
    timeoutMs: 30_000,
  })
}

export function recordSecurityDecision(payload: SecurityDecisionPayload): Promise<unknown> {
  return request<unknown>('/security/decisions', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeoutMs: 30_000,
  })
}

// ── Notifications ─────────────────────────────────────────────────────────────

export function listNotifications(limit = 100, unreadOnly = false): Promise<Notification[]> {
  return request<Notification[]>(
    `/notifications?limit=${limit}&unread_only=${unreadOnly}`,
  ).catch(() => [])
}

export function getUnreadCount(): Promise<UnreadCountResponse> {
  return request<UnreadCountResponse>('/notifications/unread-count').catch(() => ({ count: 0 }))
}

export function markNotificationRead(id: string): Promise<unknown> {
  return request<unknown>(`/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' })
}

export function markAllNotificationsRead(): Promise<unknown> {
  return request<unknown>('/notifications/read-all', { method: 'POST' })
}

export function listEgressDomains(): Promise<EgressDomainsResponse> {
  return request<EgressDomainsResponse>('/egress/domains').catch(() => ({ domains: [] }))
}

export function grantEgressDomain(domain: string): Promise<unknown> {
  return request<unknown>('/egress/domains/grant', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  })
}

export function revokeEgressDomain(domain: string): Promise<unknown> {
  return request<unknown>('/egress/domains/revoke', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  })
}

/**
 * Fetch the current egress mode plus both allow-list and deny-list.
 * Falls back to a legacy GET /egress/domains shape if the backend does not
 * yet expose GET /egress/mode (returns mode='deny' with the existing allow-list).
 */
export async function getEgressMode(): Promise<EgressModeResponse> {
  // GET /egress/domains is the source of truth for mode + BOTH lists. The /egress/mode
  // endpoint only returns {mode, description} (no lists), so reading it left domains/deny
  // undefined and crashed the panels (.length on undefined). Always normalise to arrays.
  const d = await request<{
    mode?: string
    domains?: string[]
    denylist?: string[]
    deny?: string[]
    blocklist_count?: number
  }>('/egress/domains')
  return {
    mode: d.mode === 'allow' ? 'allow' : 'deny',
    domains: Array.isArray(d.domains) ? d.domains : [],
    deny: Array.isArray(d.denylist) ? d.denylist : Array.isArray(d.deny) ? d.deny : [],
    blocklist_count: d.blocklist_count,
  }
}

/**
 * Change the egress mode.  Always requires a valid TOTP code (MFA gate).
 */
export function setEgressMode(mode: EgressMode, totp: string): Promise<unknown> {
  return request<unknown>('/egress/mode', {
    method: 'POST',
    body: JSON.stringify({ mode, totp }),
  })
}

/** Add a domain to the manual block-list (mode=allow only, no MFA required). */
export function blockEgressDomain(domain: string): Promise<unknown> {
  return request<unknown>('/egress/deny/add', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  })
}

/** Remove a domain from the manual block-list (mode=allow only, no MFA required). */
export function unblockEgressDomain(domain: string): Promise<unknown> {
  return request<unknown>('/egress/deny/remove', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  })
}

// ── Approvals (HITL) ──────────────────────────────────────────────────────────

export function listPendingApprovals(): Promise<PendingApproval[]> {
  return request<PendingApproval[]>('/approvals/pending').catch(() => [])
}

export function resolveApproval(
  proposalId: string,
  decision: string,
  factors: { totp?: string | null } = {},
): Promise<unknown> {
  return request<unknown>(`/approvals/${encodeURIComponent(proposalId)}`, {
    method: 'POST',
    body: JSON.stringify({ decision, totp: factors.totp ?? null }),
  })
}

// ── MFA enrollment ────────────────────────────────────────────────────────────

export function mfaStatus(): Promise<MfaStatus> {
  return request<MfaStatus>('/mfa/status').catch(() => ({ enrolled: false }))
}

export function mfaEnroll(totp: string | null = null): Promise<{ otpauth_uri?: string; secret?: string }> {
  return request<{ otpauth_uri?: string; secret?: string }>('/mfa/enroll', {
    method: 'POST',
    body: JSON.stringify({ totp }),
  })
}

// ── Security policies ─────────────────────────────────────────────────────────

export function getPolicies(): Promise<PoliciesResponse> {
  return request<PoliciesResponse>('/policies').catch(
    () => ({ preset: 'equilibrado', tools: {}, mfa_on_dangers: true }),
  )
}

export function setPolicyPreset(preset: string, totp: string): Promise<unknown> {
  return request<unknown>('/policies/preset', {
    method: 'POST',
    body: JSON.stringify({ preset, totp }),
  })
}

export function setPolicyTool(tool: string, enabled: boolean, totp: string): Promise<unknown> {
  return request<unknown>('/policies/tool', {
    method: 'POST',
    body: JSON.stringify({ tool, enabled, totp }),
  })
}

export function setPolicyTools(tools: Record<string, boolean>, totp: string): Promise<unknown> {
  return request<unknown>('/policies/tools', {
    method: 'POST',
    body: JSON.stringify({ tools, totp }),
  })
}

export function setMfaOnDangers(enabled: boolean, totp: string): Promise<unknown> {
  return request<unknown>('/policies/mfa_on_dangers', {
    method: 'POST',
    body: JSON.stringify({ enabled, totp }),
  })
}

// ── Memory ────────────────────────────────────────────────────────────────────

export function listMemory(): Promise<MemoryItem[]> {
  return request<MemoryItem[]>('/memory')
}

export function searchMemory(query: string): Promise<MemoryItem[]> {
  return request<MemoryItem[]>(`/memory/search?q=${encodeURIComponent(query)}`)
}

export function forgetMemoryItem(id: string): Promise<unknown> {
  return request<unknown>(`/memory/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

// ── Workspace files ───────────────────────────────────────────────────────────

/**
 * List workspace files at the given relative path.
 * GET /workspace/files?path=<relpath>
 * Returns array of { name, kind, path, is_dir, size, modified }.
 */
export function listWorkspaceFiles(path?: string): Promise<WorkspaceFile[]> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : ''
  return request<WorkspaceFile[]>(`/workspace/files${qs}`).catch(() => [])
}

/**
 * Returns the URL to download a workspace file by its relative path.
 * GET /workspace/download?path=<relpath>
 */
export function workspaceDownloadUrl(path: string): string {
  return `/api/v1/workspace/download?path=${encodeURIComponent(path)}`
}

// ── Memory — full entry fetch ─────────────────────────────────────────────────

/**
 * Fetch the full content of a single memory entry.
 * GET /memory/{entry_id}  where entry_id = "{target}:{entry_index}"
 */
export function getMemoryEntry(entryId: string): Promise<MemoryEntryDetail> {
  return request<MemoryEntryDetail>(`/memory/${encodeURIComponent(entryId)}`)
}

// ── Instance / Edition ────────────────────────────────────────────────────────

export interface InstanceFeatures {
  edition: 'community' | 'associate'
  /** Identifiers of views the current user may access. CE backend returns all views. */
  views: string[]
}

/**
 * Returns the edition and the list of allowed view identifiers.
 * Never throws — callers normalise with ?? [] on the views array.
 */
export function getInstanceFeatures(): Promise<InstanceFeatures> {
  return request<InstanceFeatures>('/instance/features')
}

// ── Usage / Cost ──────────────────────────────────────────────────────────────

export function getUsageSummary(period: UsagePeriod): Promise<UsageSummary> {
  return request<UsageSummary>(`/usage/summary?period=${encodeURIComponent(period)}`).catch(() => ({
    available: false,
    period,
    currency: 'USD',
    total_cost_usd: 0,
    projected_cost_usd: 0,
    total_tokens: 0,
    cycles: 0,
    failures: 0,
    self_hosted_cycles: 0,
    top_models: [],
  }))
}

export function getUsageByAgent(period: UsagePeriod): Promise<UsageByAgent> {
  return request<UsageByAgent>(`/usage/by-agent?period=${encodeURIComponent(period)}`).catch(() => ({
    available: false,
    agents: [],
  }))
}

export function getUsageTimeseries(period: UsagePeriod, dimension: UsageDimension): Promise<UsageTimeseries> {
  return request<UsageTimeseries>(
    `/usage/timeseries?period=${encodeURIComponent(period)}&dimension=${encodeURIComponent(dimension)}`,
  ).catch(() => ({
    available: false,
    points: [],
  }))
}

export function getConversationUsage(id: string): Promise<ConversationUsage> {
  return request<ConversationUsage>(`/chat/conversations/${encodeURIComponent(id)}/usage`)
}

// ── WebSocket stream ──────────────────────────────────────────────────────────

export interface StreamCallbacks {
  onDelta(text: string): void
  onThinking(text: string): void
  onToolCall(frame: Extract<StreamFrame, { kind: 'tool_call' }>): void
  onStatus(message: string): void
  onDone(): void
  onError(message: string): void
}

interface StreamHandle {
  close(): void
}

/**
 * Opens a WebSocket stream for a given task_id.
 *
 * Protocol (mirrors vanilla stream.js):
 *   WS  ws[s]://<host>/api/v1/chat/stream/{task_id}
 *   No token in URL — same-origin; token is NOT in the WS URL per vanilla design
 *   (api.js sends Bearer on HTTP; the WS is same-origin and the daemon doesn't
 *    require auth on the streaming socket itself).
 *
 * Frame kinds: delta | thinking_delta | tool_call | status | done | error
 */
export function openTaskStream(
  taskId: string,
  callbacks: StreamCallbacks,
  opts: { maxRetries?: number } = {},
): StreamHandle {
  const { maxRetries = 8 } = opts
  const wsBase = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`
  const wsPath = '/api/v1/chat/stream'

  let ws: WebSocket | null = null
  let retries = 0
  let closed = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null

  // lastSeq survives reconnects for this task handle: the broker replays the
  // entire run on re-subscribe, so we discard frames whose seq we already saw.
  // Frames without seq (older backend) always pass through (seq undefined).
  let lastSeq = -1

  function connect() {
    if (closed) return
    const url = `${wsBase}${wsPath}/${encodeURIComponent(taskId)}`
    ws = new WebSocket(url)

    ws.addEventListener('message', (event) => {
      retries = 0
      let frame: StreamFrame
      try {
        frame = JSON.parse(event.data as string) as StreamFrame
      } catch {
        return
      }

      // Dedup by seq: discard frames we already processed (replayed by the broker
      // after a reconnect). Frames without seq pass through unconditionally.
      const frameSeq = (frame as Record<string, unknown>).seq
      if (typeof frameSeq === 'number') {
        if (frameSeq <= lastSeq) return
        lastSeq = frameSeq
      }

      dispatch(frame)
    })

    ws.addEventListener('error', () => {
      // onclose will fire next with the code
    })

    ws.addEventListener('close', (event) => {
      if (closed) return
      if (event.code === 1000) {
        callbacks.onDone()
        return
      }
      if (retries < maxRetries) {
        retries++
        callbacks.onStatus('Reconectando con el agente…')
        const delay = Math.min(400 * 2 ** retries, 10_000)
        retryTimer = setTimeout(connect, delay)
      } else {
        callbacks.onError(
          'Se perdió la conexión con el agente. La tarea sigue en marcha; vuelve a abrir la conversación para reconectar.',
        )
      }
    })
  }

  function dispatch(frame: StreamFrame) {
    // Stay tolerant of payload variants across protocol versions (mirrors vanilla dispatch):
    // the daemon nests the chunk text in `frame.payload.delta` — without this fallback the
    // assistant bubble renders empty even though the backend streamed the reply.
    const f = frame as Record<string, unknown>
    const p = f.payload && typeof f.payload === 'object' ? (f.payload as Record<string, unknown>) : null
    const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)
    const deltaText = str(f.delta) ?? str(f.text) ?? str(p?.delta) ?? str(p?.text) ?? ''
    switch (frame.kind) {
      case 'delta': {
        callbacks.onDelta(deltaText)
        break
      }
      case 'thinking_delta': {
        callbacks.onThinking(str(f.thinking) ?? deltaText)
        break
      }
      case 'tool_call':
        callbacks.onToolCall(frame)
        break
      case 'status':
        callbacks.onStatus(str(f.message) ?? str(f.status) ?? str(p?.message) ?? '')
        break
      case 'done':
        callbacks.onDone()
        break
      case 'error':
        callbacks.onError(str(f.message) ?? 'Error desconocido del agente')
        break
    }
  }

  connect()

  return {
    close() {
      closed = true
      if (retryTimer !== null) clearTimeout(retryTimer)
      if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000)
    },
  }
}
