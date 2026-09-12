import { token, refreshToken, getAuthStatus } from '../lib/token'
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
  ManagedRemoteEndpointsResponse,
  ConfiguredTasksResponse,
  RecentTasksResponse,
  CreateTaskPayload,
  ConfiguredTask,
  SecurityScan,
  AuditHead,
  EgressDomainsResponse,
  EgressMode,
  EgressModeResponse,
  TailnetStatus,
  SshHostsResponse,
  KillSwitchStatus,
  PendingApproval,
  InboundDelegation,
  PoliciesResponse,
  InstallDecisionPayload,
  WorkspaceFile,
  MemoryItem,
  MemoryEntryDetail,
  Notification,
  UnreadCountResponse,
  InstallScanResponse,
  SecurityDecisionPayload,
  SecurityDecisionResponse,
  SkillDetails,
  UsageSummary,
  UsageByAgent,
  UsageTimeseries,
  ConversationUsage,
  UsagePeriod,
  UsageDimension,
  AdsBridgeSessionResponse,
  HostVerb,
  InstallRequestResponse,
  InstallRequestsListResponse,
  VersionSet,
  UpdatePiece,
} from './types'

// Mirrors the timeout strategy in vanilla api.js: snappy GETs fail fast;
// long-running mutations get explicit larger timeouts.
const DEFAULT_TIMEOUT_MS = 20_000
const BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown
  readonly code: string | undefined

  constructor(message: string, status: number, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.code = errorCode(body)
  }
}

function errorCode(body: unknown): string | undefined {
  if (!body || typeof body !== 'object') return undefined
  const detail = (body as Record<string, unknown>).detail
  const code = detail && typeof detail === 'object'
    ? (detail as Record<string, unknown>).code : undefined
  return typeof code === 'string' ? code : undefined
}

const FACTOR_ERRORS = new Set([
  'mfa_required', 'invalid_totp', 'mfa_not_enrolled', 'invalid_owner_approval',
])

interface RequestOptions extends RequestInit {
  timeoutMs?: number
}

async function request<T>(path: string, options: RequestOptions = {}, _retried = false): Promise<T> {
  // 028 FR-012/SC-012: once we know the bearer is gone (no token was ever present,
  // or a prior refresh definitively failed), every caller short-circuits HERE,
  // before fetch() — this is what turns "session lost" into zero further
  // /api/v1/* calls instead of every polling hook 401-ing forever. The app shell
  // (App.tsx) reacts to the same auth status by swapping to the reconnect screen.
  if (getAuthStatus().kind === 'unauthenticated' && path !== '/session/refresh') {
    throw new ApiError('No hay una sesión activa.', 401, null)
  }

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

  let errorBody: unknown = null
  if (!res.ok) {
    try { errorBody = await res.json() } catch { /* non-JSON */ }
  }

  // A rejected action approval is not an expired session. Never replay its
  // single-use grant or disturb the authenticated owner's session.
  // Session token rotated/expired mid-use → renew once and retry, so the user
  // never hits a dead 401 while the tab is active.
  if (res.status === 401 && !FACTOR_ERRORS.has(errorCode(errorBody) ?? '')
    && !_retried && token() && path !== '/session/refresh') {
    if (await refreshToken()) {
      return request<T>(path, options, true)
    }
  }

  if (!res.ok) {
    const body = errorBody
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
  provider_id: string
  api_key: string
  model?: string
  set_active?: boolean
}): Promise<Provider> {
  return request<Provider>('/providers/native', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** The native provider currently set as the model (separate store from the repo).
 *  Returns null when none is configured. Merged into the configured list by the UI. */
export function getNativeActive(): Promise<Provider | null> {
  return request<Provider | Record<string, never>>('/providers/native/active')
    .then(p => {
      if (p === null || (p && typeof p === 'object' && !Array.isArray(p) && Object.keys(p).length === 0)) return null
      if (p && typeof p.provider_id === 'string' && p.provider_id) return p as Provider
      throw new ApiError('No se pudo verificar el proveedor activo.', 502, null)
    })
}

export function setActiveProvider(providerId: string): Promise<unknown> {
  return request<unknown>(`/providers/${encodeURIComponent(providerId)}/activate`, { method: 'POST' })
}

/** `code` (PROV-03, specs/025-safent-repaso): honest classification of a
 *  non-ok result — "invalid_key" (endpoint reachable, credential rejected),
 *  "endpoint_error" (wrong base_url/path, e.g. a 404), or undefined for an
 *  unclassified provider error (still surfaced via `error`). */
export function testProvider(
  providerId: string,
): Promise<{ ok?: boolean; error?: string | null; code?: 'invalid_key' | 'endpoint_error' | null }> {
  return request<{ ok?: boolean; error?: string | null; code?: 'invalid_key' | 'endpoint_error' | null }>(
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
  )
}

// ── Skills ────────────────────────────────────────────────────────────────────

export function listSkills(): Promise<Skill[]> {
  return request<Skill[]>('/skills')
}

export function searchSkillsHub(query: string): Promise<{ results?: HubSkillResult[] } | HubSkillResult[]> {
  return request<{ results?: HubSkillResult[] } | HubSkillResult[]>(
    `/skills/hub/search?q=${encodeURIComponent(query)}`,
  )
}

export function listHubSkills(): Promise<HubSkillResult[]> {
  return request<HubSkillResult[]>('/skills/hub')
}

export function installSkill(
  identifier: string,
  force = false,
  approvalGrant?: string,
): Promise<HubInstallResponse> {
  return request<HubInstallResponse>('/skills/hub/install', {
    method: 'POST',
    body: JSON.stringify({ identifier, force }),
    // Exact-action, single-use confirmation from POST /security/decisions.
    ...(approvalGrant ? { headers: { 'X-Owner-Approval-Grant': approvalGrant } } : {}),
  })
}

export function getHubOpStatus(opId: string): Promise<HubOpStatus> {
  return request<HubOpStatus>(`/skills/hub/ops/${encodeURIComponent(opId)}`)
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

export function addMcpServer(payload: Record<string, unknown>, approvalGrant?: string): Promise<McpAddResponse> {
  // The daemon connects eagerly; a rejection (bad draft, disallowed runner,
  // security-scan block, ...) is now a 400/403 — request<T>'s !res.ok branch
  // throws ApiError(message, status, body) with the daemon's {ok, error, ...}
  // under body.detail (see mcp_api.py._raise_if_failed). tool_count===0 on a
  // genuine success (connected but exposes no tools) is NOT a failure, so it
  // still resolves — callers surface that warning separately.
  return request<McpAddResponse>('/mcp', {
    method: 'POST',
    headers: approvalGrant ? { 'X-Owner-Approval-Grant': approvalGrant } : undefined,
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

export function listManagedRemoteEndpoints(): Promise<ManagedRemoteEndpointsResponse> {
  return request<ManagedRemoteEndpointsResponse>('/mcp/managed-remote-endpoints')
    .catch(() => ({ endpoints: {} }))
}

export function connectManagedRemote(slug: string, url: string, force = false, approvalGrant?: string): Promise<McpAddResponse> {
  return request<McpAddResponse>(`/mcp/managed-remote/${encodeURIComponent(slug)}/connect`, {
    method: 'POST',
    headers: approvalGrant ? { 'X-Owner-Approval-Grant': approvalGrant } : undefined,
    body: JSON.stringify({ url, force }),
    timeoutMs: 300_000,
  })
}

// ── Ads bridge (026, contracts/sso.md) ──────────────────────────────────────
// Mints/refreshes the `ads_bridge` cookie (same-origin, HttpOnly — invisible
// to this client) AND reports companion readiness in one round trip, so the
// sidebar's poll both keeps the bridge warm (SC-002: zero-second logins)
// and drives the disabled/enabled state. Fail-soft: a transient network
// error degrades to "unavailable/unreachable", never a thrown exception —
// useAdsAvailability keeps the last known state instead.
export function mintAdsBridgeSession(): Promise<AdsBridgeSessionResponse> {
  return request<AdsBridgeSessionResponse>('/ads/bridge/session', { method: 'POST' })
    .catch(() => ({ status: 'unavailable', reason: 'unreachable' }))
}

// ── Install requests (028/029, contracts/install-request.md) ───────────────────
// The sandbox leaves a marker; the host agent (or the app itself) claims and
// fulfils it. Shared by the Ads companion install/repair action (029) and the
// system update/uninstall footer (028) — one contract, one client surface.

export function postInstallRequest(
  verb: HostVerb,
  opts: { slug?: 'safent-ads'; retention?: 'keep' | 'purge' } = {},
): Promise<InstallRequestResponse> {
  return request<InstallRequestResponse>('/system/requests', {
    method: 'POST',
    body: JSON.stringify({ verb, ...opts }),
  }).catch((e) => {
    // 409 = "a live request for this verb already exists" — the contract's own
    // idempotency signal (install-request.md §1.5), not a failure: the caller
    // adopts the existing request instead of showing an error (FR-008).
    if (e instanceof ApiError && e.status === 409 && e.body && typeof e.body === 'object') {
      return e.body as InstallRequestResponse
    }
    throw e
  })
}

export function getInstallRequests(): Promise<InstallRequestsListResponse> {
  return request<InstallRequestsListResponse>('/system/requests')
}

// ── Tasks ─────────────────────────────────────────────────────────────────────

export function getTaskDashboard(): Promise<import('./types').TaskDashboardResponse> {
  return request('/tasks/dashboard?limit=100')
}

/** No empty fallback: transport failure is not an empty inbox. */
export function getTaskInbox(): Promise<InboundDelegation[]> {
  return request('/inbound-delegations')
}

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

/**
 * Stop a running task (cooperative cancel). Terminal, no retry.
 * POST /tasks/{task_id}/cancel
 */
export function cancelTask(taskId: string): Promise<{ ok: boolean; requested?: boolean }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', body: '{}' })
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
  return request<ConversationSummary[]>(`/chat/conversations${qs}`)
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

export function recordSecurityDecision(
  payload: SecurityDecisionPayload,
): Promise<SecurityDecisionResponse> {
  return request<SecurityDecisionResponse>('/security/decisions', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeoutMs: 30_000,
  })
}

// ── Notifications ─────────────────────────────────────────────────────────────

export function listNotifications(limit = 100, unreadOnly = false): Promise<Notification[]> {
  return request<Notification[]>(
    `/notifications?limit=${limit}&unread_only=${unreadOnly}`,
  )
}

export function getUnreadCount(): Promise<UnreadCountResponse> {
  return request<UnreadCountResponse>('/notifications/unread-count')
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
export function setEgressMode(mode: EgressMode): Promise<unknown> {
  return request<unknown>('/egress/mode', {
    method: 'POST',
    body: JSON.stringify({ mode }),
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

// ── Governed tailnet (spec 022) ─────────────────────────────────────────────

const TAILNET_UNCONFIGURED: TailnetStatus = {
  configured: false,
  online: false,
  node_name: null,
  magicdns_suffix: null,
  tailnet: null,
  peers: [],
  last_attempt: null,
}

/** Current tailnet status. Falls back to "not configured" on any fetch error
 * (mirrors listEgressDomains) so a transient backend hiccup never crashes the card. */
export function getTailnetStatus(): Promise<TailnetStatus> {
  return request<TailnetStatus>('/tailnet').catch(() => TAILNET_UNCONFIGURED)
}

export function getTailnetPeers(): Promise<{ peers: TailnetStatus['peers'] }> {
  return request<{ peers: TailnetStatus['peers'] }>('/tailnet/peers').catch(() => ({ peers: [] }))
}

/** Stage a tailnet connect. The key is never echoed back by the backend. */
export function connectTailnet(authKey: string): Promise<{ staged: boolean }> {
  return request<{ staged: boolean }>('/tailnet/connect', {
    method: 'POST',
    body: JSON.stringify({ auth_key: authKey }),
  })
}

/** Stage a tailnet disconnect — gated by the device password (PAM, root helper). */
export function disconnectTailnet(password: string): Promise<{ staged: boolean }> {
  return request<{ staged: boolean }>('/tailnet/disconnect', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
}

/** Hosts approved for governed SSH (spec 022 v2). Fail-soft: an empty list on
 * fetch error, never a crash (mirrors getTailnetStatus/getTailnetPeers above). */
export function getSshHosts(): Promise<SshHostsResponse> {
  return request<SshHostsResponse>('/tailnet/ssh-hosts').catch(() => ({ hosts: [] }))
}

/** Revoke a host's governed-SSH approval — requires the owner's TOTP. */
export function revokeSshHost(host: string): Promise<SshHostsResponse> {
  return request<SshHostsResponse>(`/tailnet/ssh-hosts/${encodeURIComponent(host)}`, {
    method: 'DELETE',
    body: JSON.stringify({}),
  })
}

/** Emergency brake status. Fail-soft: never throws, defaults to not-engaged. */
export function getKillSwitch(): Promise<KillSwitchStatus> {
  return request<KillSwitchStatus>('/security/kill-switch')
}

/** Engage the brake — no MFA required, one click (it's a brake). */
export function engageKillSwitch(reason: string): Promise<unknown> {
  return request<unknown>('/security/kill-switch', {
    method: 'POST',
    body: JSON.stringify({ engaged: true, reason }),
  })
}

/** Release the brake after explicit owner confirmation. No Community MFA. */
export function releaseKillSwitch(): Promise<unknown> {
  return request<unknown>('/security/kill-switch', {
    method: 'POST', body: JSON.stringify({ engaged: false }),
  })
}

// ── Approvals (HITL) ──────────────────────────────────────────────────────────

export function listPendingApprovals(): Promise<PendingApproval[]> {
  return request<PendingApproval[]>('/approvals/pending').catch(() => [])
}

export function resolveApproval(
  proposalId: string,
  decision: 'once' | 'deny',
): Promise<unknown> {
  return request<unknown>(`/approvals/${encodeURIComponent(proposalId)}`, {
    method: 'POST',
    body: JSON.stringify({ decision }),
  })
}

// ── Inbound cross-human delegations (FASE 3 A2A) ─────────────────────────────

export function listInboundDelegations(): Promise<InboundDelegation[]> {
  return request<InboundDelegation[]>('/inbound-delegations').catch(() => [])
}

export function resolveInboundDelegation(
  messageId: string,
  decision: 'approve' | 'reject',
): Promise<{ ok: boolean; task_id?: string | null }> {
  return request<{ ok: boolean; task_id?: string | null }>(
    `/inbound-delegations/${encodeURIComponent(messageId)}`,
    { method: 'POST', body: JSON.stringify({ decision }) },
  )
}

// ── Security policies ─────────────────────────────────────────────────────────

export function getPolicies(): Promise<PoliciesResponse> {
  return request<PoliciesResponse>('/policies')
}

export function setPolicyPreset(preset: string): Promise<unknown> {
  return request<unknown>('/policies/preset', {
    method: 'POST',
    body: JSON.stringify({ preset }),
  })
}

export function setPolicyTool(tool: string, enabled: boolean): Promise<unknown> {
  return request<unknown>('/policies/tool', {
    method: 'POST',
    body: JSON.stringify({ tool, enabled }),
  })
}

export function setPolicyTools(tools: Record<string, boolean>): Promise<unknown> {
  return request<unknown>('/policies/tools', {
    method: 'POST',
    body: JSON.stringify({ tools }),
  })
}

export function setApprovalOnDangers(enabled: boolean): Promise<unknown> {
  return request<unknown>('/policies/approval_on_dangers', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
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
  return request<WorkspaceFile[]>(`/workspace/files${qs}`)
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

/**
 * Edit the content of a single memory entry.
 * PUT /memory/{entry_id}  body { content }
 * Rejects (400) if the new content trips the PII/injection guard.
 */
export function updateMemoryEntry(
  entryId: string,
  content: string,
): Promise<{ ok: boolean; updated?: boolean }> {
  return request(`/memory/${encodeURIComponent(entryId)}`, {
    method: 'PUT',
    body: JSON.stringify({ content }),
  })
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

// ── System update ─────────────────────────────────────────────────────────────

// Rich shape per contracts/update.md §3 — "current_version", "latest_version" and
// "update_available" are the pre-028 fields (conserved for back-compat: expandir
// → contraer, never a hard cutover); "current"/"to"/"pieces"/"checked_at" mirror
// the window.__safentUpdate object the Tauri host shell injects once it has
// actually checked (the daemon's own check can be blocked by the egress cage).
export interface SystemUpdateStatus {
  current_version: string
  latest_version: string | null
  update_available: boolean
  updating: boolean
  available?: boolean
  current?: VersionSet
  to?: VersionSet
  pieces?: UpdatePiece[]
  checked_at?: string
}

/** A failed check is unknown, not evidence that no update exists. */
export function getSystemUpdate(): Promise<SystemUpdateStatus> {
  return request<SystemUpdateStatus>('/system/update')
}

/** Drops an uninstall marker; the host `safent agent` runs `safent uninstall` (removes the
 *  container, data volume, CLI and agent — keeps podman/docker). Same mechanism as update. */
export function requestSystemUninstall(): Promise<{ ok: boolean }> {
  return request('/system/uninstall', { method: 'POST', body: JSON.stringify({}) })
}

// ── Usage / Cost ──────────────────────────────────────────────────────────────

export function getUsageSummary(period: UsagePeriod): Promise<UsageSummary> {
  return request<UsageSummary>(`/usage/summary?period=${encodeURIComponent(period)}`)
}

export function getUsageByAgent(period: UsagePeriod): Promise<UsageByAgent> {
  return request<UsageByAgent>(`/usage/by-agent?period=${encodeURIComponent(period)}`)
}

export function getUsageTimeseries(period: UsagePeriod, dimension: UsageDimension): Promise<UsageTimeseries> {
  return request<UsageTimeseries>(
    `/usage/timeseries?period=${encodeURIComponent(period)}&dimension=${encodeURIComponent(dimension)}`,
  )
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
 * Opens an SSE (EventSource) stream for a given task_id.
 *
 * Protocol: GET <same-origin>/api/v1/chat/stream/{task_id}, media text/event-stream.
 * SSE is the LLM-streaming protocol (OpenAI/Anthropic) and gives us NATIVE resume:
 * the browser auto-reconnects on any drop and re-sends `Last-Event-ID` (= the daemon
 * per-task `seq` we put in each event's `id:`); the server replays only the missed
 * frames from the broker log. Resume is the PROTOCOL's job — no bespoke reconnect/
 * backoff/replay here (that fragility was the recurring "chat dies on refresh" bug).
 * Same-origin GET, session-token-gated like every other /api/v1/* route. EventSource
 * cannot set an Authorization header, so the bearer travels as `?token=` instead —
 * same credential, alternate transport (see main.py's `_require_operator_token`).
 *
 * Frame kinds: delta | thinking_delta | tool_call | status | done | error
 */
export function openTaskStream(
  taskId: string,
  callbacks: StreamCallbacks,
  _opts: { maxRetries?: number } = {},
): StreamHandle {
  const path = `/api/v1/chat/stream/${encodeURIComponent(taskId)}?token=${encodeURIComponent(token())}`
  let es: EventSource | null = new EventSource(path)
  let closed = false
  // Defensive dedup; the server already filters by Last-Event-ID so this rarely fires.
  let lastSeq = -1

  function finish() {
    closed = true
    if (es) { es.close(); es = null }
  }

  es.onmessage = (event: MessageEvent) => {
    let frame: StreamFrame
    try {
      frame = JSON.parse(event.data as string) as StreamFrame
    } catch {
      return
    }
    const frameSeq = (frame as Record<string, unknown>).seq
    if (typeof frameSeq === 'number') {
      if (frameSeq <= lastSeq) return
      lastSeq = frameSeq
    }
    dispatch(frame)
  }

  es.onerror = () => {
    // EventSource reconnects AUTOMATICALLY on a transient drop (it does not give up,
    // and re-sends Last-Event-ID). Do NOT close or raise a fatal error: the task keeps
    // running server-side and the server replays on re-attach. A real terminal task
    // error arrives as a `kind:error` FRAME (handled in dispatch → finish()), not here.
    if (!closed) callbacks.onStatus('Reconectando con el agente…')
  }

  function dispatch(frame: StreamFrame) {
    // Stay tolerant of payload variants across protocol versions: the daemon nests the
    // chunk text in `frame.payload.delta` — without this fallback the assistant bubble
    // renders empty even though the backend streamed the reply.
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
        finish()  // close the EventSource so it does NOT auto-reconnect after the end
        callbacks.onDone()
        break
      case 'error':
        finish()
        callbacks.onError(str(f.message) ?? str(f.error) ?? str(p?.error) ?? 'Error desconocido del agente')
        break
    }
  }

  return {
    close() {
      finish()
    },
  }
}

// ── Clipboard bridge for the noVNC view (proxies the jail's xclip server) ──────
// Read the jailed browser's X CLIPBOARD (poll → mirror into the local clipboard).
export async function getBrowserClipboard(): Promise<{ ok: boolean; text: string }> {
  return request('/clipboard', { method: 'GET' })
}

// Set the jailed browser's X CLIPBOARD (then the UI injects a real Ctrl+V over RFB).
export async function setBrowserClipboard(text: string): Promise<{ ok: boolean }> {
  return request('/clipboard', { method: 'POST', body: JSON.stringify({ text }) })
}
