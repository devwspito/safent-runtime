/**
 * api.js — Thin fetch wrapper for the Hermes shell-server API.
 *
 * All requests are to the same origin (served by FastAPI).
 * Errors surface as ApiError instances; callers can catch or let them
 * bubble to the global error handler for toast display.
 *
 * Security: no auth tokens in URLs; same-origin only; CSRF-safe (SameSite
 * cookies + same-origin fetch). URL user inputs are never used in fetch paths.
 */

export class ApiError extends Error {
  /** @param {string} message @param {number} status @param {unknown} body */
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

const BASE = '/api/v1';

// Default per-request timeout for snappy view-load GETs. A desktop app must NEVER
// hang for minutes on a slow/stuck endpoint — these fail fast so the view shows an
// empty/error state in seconds instead of a perpetual skeleton. Operations that
// legitimately take longer (testing a provider does a REAL model completion ~10-40s;
// sending a chat; adding an MCP server) pass a larger timeoutMs explicitly — capping
// THOSE at 12s was aborting valid requests and surfacing as "sin respuesta".
const DEFAULT_TIMEOUT_MS = 20000;

/**
 * Core fetch wrapper. Returns parsed JSON or throws ApiError.
 * @param {string} path
 * @param {RequestInit & { timeoutMs?: number }} [init]
 */
async function request(path, init = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...rest } = init;
  const headers = { 'Content-Type': 'application/json', ...(rest.headers ?? {}) };
  // Operator token (V-1): injected into index.html by the shell-server; required
  // on all mutating /api/v1 routes. Sent on every request (harmless on GETs).
  const _tok = (typeof window !== 'undefined' && window.__LUMEN_TOKEN__) || '';
  if (_tok && !headers.Authorization) headers.Authorization = `Bearer ${_tok}`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${BASE}${path}`, { ...rest, headers, signal: ctrl.signal });
  } catch (err) {
    clearTimeout(timer);
    if (err?.name === 'AbortError') {
      throw new ApiError(`La petición tardó demasiado (${Math.round(timeoutMs / 1000)}s) y se canceló.`, 0, null);
    }
    throw new ApiError(`Error de red: ${err.message}`, 0, null);
  }
  clearTimeout(timer);

  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON body */ }
    const message = body?.detail?.message ?? body?.detail ?? `HTTP ${res.status}`;
    throw new ApiError(message, res.status, body);
  }

  if (res.status === 204) return null;
  return res.json();
}

// ── Chat ─────────────────────────────────────────────────────────────────────

/**
 * Enqueue a chat message. Returns {task_id, stream_path}.
 * @param {{conversation_id?: string, user_message: string, dedup_key?: string}} payload
 */
export function postChat(payload) {
  // Enqueue only — returns a task_id in ~20ms; the actual work (seconds or HOURS)
  // streams over the WebSocket, which is never timed out. Uses the default backstop
  // purely so a dead socket fails fast instead of spinning the send button forever.
  return request('/chat', { method: 'POST', body: JSON.stringify(payload) });
}

/** List conversation summaries. */
export function listConversations(agent_id) {
  const qs = agent_id ? `?agent_id=${encodeURIComponent(agent_id)}` : '';
  return request(`/chat/conversations${qs}`).catch(_emptyOnNotFound([]));
}

/** Get conversation detail (messages). */
export function getConversation(id) {
  return request(`/chat/conversations/${encodeURIComponent(id)}`);
}

/** Delete a conversation. */
export function deleteConversation(id) {
  return request(`/chat/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ── Tasks ────────────────────────────────────────────────────────────────────

export function listRecentTasks(limit = 50) {
  return request(`/tasks/recent?limit=${limit}`).catch(() => ({ available: false, tasks: [] }));
}

export function listConfiguredTasks() {
  return request('/tasks/configured').catch(() => ({ available: false, tasks: [] }));
}

export function createTask(payload) {
  return request('/tasks/scheduled', { method: 'POST', body: JSON.stringify(payload) });
}

export function deleteTask(taskId) {
  return request(`/tasks/scheduled/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
}

export function toggleTask(taskId, enabled) {
  return request(`/tasks/scheduled/${encodeURIComponent(taskId)}/enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  });
}

// ── Providers ────────────────────────────────────────────────────────────────

/** Configured providers. */
export function listProviders() {
  return request('/providers').catch(_emptyOnNotFound([]));
}

/** Full native Hermes provider catalog (42+ providers). */
export function listNativeProviders() {
  return request('/providers/native').catch(_emptyOnNotFound([]));
}

/** Add a provider. */
export function addProvider(payload) {
  return request('/providers', { method: 'POST', body: JSON.stringify(payload) });
}

/** Set active provider. */
export function setActiveProvider(providerId) {
  return request(`/providers/${encodeURIComponent(providerId)}/activate`, { method: 'POST' });
}

/** Test a provider connection. Does a REAL minimal completion via Nous (~10-40s). */
export function testProvider(providerId) {
  return request(`/providers/${encodeURIComponent(providerId)}/test`, { method: 'POST', timeoutMs: 60000 });
}

/** Start OAuth for a provider. Returns the daemon dict:
 *  loopback   → { session_id, flow:"loopback", auth_url, expires_in }
 *  device-code→ { session_id, user_code, verification_url, expires_in, poll_interval }
 *  or { error }.
 */
export function startProviderOAuth(providerId) {
  return request(`/providers/${encodeURIComponent(providerId)}/oauth/start`, { method: 'POST' });
}

/** Poll OAuth status by SESSION id (backend route: GET /providers/oauth/{session_id}). */
export function getProviderOAuthStatus(sessionId) {
  return request(`/providers/oauth/${encodeURIComponent(sessionId)}`).catch(() => ({ status: 'unknown' }));
}

// ── Skill teaching / recording (training) ──────────────────────────────────────
// Mirrors the native SO: create a session (browser surface), start recording the
// demonstration, stop, then sign → the skill is compiled and added (validated).

/** Create a teaching session. Returns TrainingState { session_id, state, ... }. */
export function createTrainingSession({ skill_name, description, surface_kind = 'browser' }) {
  return request('/training', { method: 'POST', body: JSON.stringify({ skill_name, description, surface_kind }) });
}
export function startTrainingRecording(sessionId) {
  return request(`/training/${encodeURIComponent(sessionId)}/start`, { method: 'POST', body: '{}' });
}
export function stopTrainingRecording(sessionId) {
  return request(`/training/${encodeURIComponent(sessionId)}/stop`, { method: 'POST', body: '{}' });
}
export function signTrainingSession(sessionId) {
  return request(`/training/${encodeURIComponent(sessionId)}/sign`, { method: 'POST', body: '{}' });
}
/** Synthesize a real SKILL.md from the demonstration via the active LLM. */
export function synthesizeSkill(sessionId) {
  return request(`/training/${encodeURIComponent(sessionId)}/synthesize`, { method: 'POST', body: '{}' });
}
export function abandonTrainingSession(sessionId) {
  return request(`/training/${encodeURIComponent(sessionId)}/abandon`, { method: 'POST', body: '{}' }).catch(() => ({}));
}

/** Delete a provider. */
export function deleteProvider(providerId) {
  return request(`/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
}

// ── Agents ───────────────────────────────────────────────────────────────────

export function listAgents() {
  return request('/agents').catch(_emptyOnNotFound([]));
}

export function getAgent(agentId) {
  return request(`/agents/${encodeURIComponent(agentId)}`);
}

export function createAgent(payload) {
  return request('/agents', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateAgent(agentId, payload) {
  return request(`/agents/${encodeURIComponent(agentId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function deleteAgent(agentId) {
  return request(`/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' });
}

export function setActiveAgent(agentId) {
  return request(`/agents/${encodeURIComponent(agentId)}/activate`, { method: 'POST' });
}

/** Which agent is currently active. Returns {active_agent_id}. */
export function getActiveAgent() {
  return request('/agents/active').catch(() => ({ active_agent_id: '' }));
}

// Per-agent capability binding (skills + MCP) — SO parity.
export function listAgentCapabilities(agentId) {
  return request(`/agents/${encodeURIComponent(agentId)}/capabilities`).catch(_emptyOnNotFound([]));
}
export function bindAgentCapability(agentId, { kind, capability_id }) {
  return request(`/agents/${encodeURIComponent(agentId)}/capabilities`, {
    method: 'POST', body: JSON.stringify({ kind, capability_id }),
  });
}
export function unbindAgentCapability(agentId, capabilityId, kind) {
  return request(`/agents/${encodeURIComponent(agentId)}/capabilities/${encodeURIComponent(capabilityId)}?kind=${encodeURIComponent(kind)}`, { method: 'DELETE' });
}

// Per-agent Composio connection binding.
export function listAgentComposio(agentId) {
  return request(`/agents/${encodeURIComponent(agentId)}/composio`).catch(_emptyOnNotFound([]));
}
export function listComposioConnections() {
  return request('/composio/connections').catch(_emptyOnNotFound([]));
}
export function bindAgentComposio(agentId, connectionId, toolkitSlug = '') {
  return request(`/agents/${encodeURIComponent(agentId)}/composio`, {
    method: 'POST', body: JSON.stringify({ connection_id: connectionId, toolkit_slug: toolkitSlug }),
  });
}
export function unbindAgentComposio(agentId, connectionId) {
  return request(`/agents/${encodeURIComponent(agentId)}/composio/${encodeURIComponent(connectionId)}`, { method: 'DELETE' });
}

// ── Skills ───────────────────────────────────────────────────────────────────

export function listSkills() {
  return request('/skills').catch(_emptyOnNotFound([]));
}

export function searchSkillsHub(query) {
  return request(`/skills/hub/search?q=${encodeURIComponent(query)}`).catch(_emptyOnNotFound([]));
}

/** List skills already installed from the hub (for "installed" dedup on results). */
export function listHubSkills() {
  return request('/skills/hub').catch(_emptyOnNotFound([]));
}

/** Install a hub skill. Body MUST be {identifier}. Returns 202 {op_id, status}. */
export function installSkill(identifier) {
  return request('/skills/hub/install', { method: 'POST', body: JSON.stringify({ identifier }) });
}

/** Poll an async hub install/uninstall operation. */
export function getHubOpStatus(opId) {
  return request(`/skills/hub/ops/${encodeURIComponent(opId)}`).catch(() => ({ status: 'unknown' }));
}

/** Uninstall a hub skill by name. Returns 202 {op_id, status}. */
export function uninstallHubSkill(name) {
  return request(`/skills/hub/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

/** Promote a validated skill to autonomous. Path id is the package_id; body {confirm:true}. */
export function promoteSkill(packageId) {
  return request(`/skills/${encodeURIComponent(packageId)}/promote`, {
    method: 'POST', body: JSON.stringify({ confirm: true }),
  });
}

// ── Integrations (Composio) ───────────────────────────────────────────────────

export function listComposioConnected() {
  return request('/integrations/composio/connected').catch(_emptyOnNotFound([]));
}

export function getComposioStatus() {
  return request('/integrations/composio/status').catch(() => ({ has_key: false, enabled: false, entity_id: '' }));
}

export function listComposioApps() {
  return request('/integrations/composio/toolkits').catch(_emptyOnNotFound([]));
}

export function connectComposioApp(slug) {
  return request('/integrations/composio/connect', {
    method: 'POST',
    body: JSON.stringify({ toolkit_slug: slug }),
  });
}

export function setComposioApiKey(apiKey) {
  return request('/integrations/composio/key', {
    method: 'POST',
    body: JSON.stringify({ api_key: apiKey }),
  });
}

// ── MCP ──────────────────────────────────────────────────────────────────────

export function listMcpServers() {
  return request('/mcp').catch(_emptyOnNotFound([]));
}

export async function addMcpServer(payload) {
  // The daemon connects the server eagerly and reports failures as
  // {ok:false, error} with a 201 status (not an HTTP error), so a bad runner,
  // a blocked scan, an invalid env or a failed handshake would otherwise pass
  // silently. Surface that as a thrown ApiError so the caller's toast fires.
  // Connecting a server (npx/uvx download + MCP handshake) can take minutes on a
  // first run, so this mutation gets a long timeout, unlike the snappy GETs.
  const res = await request('/mcp', { method: 'POST', body: JSON.stringify(payload), timeoutMs: 300000 });
  if (res && res.ok === false) {
    throw new ApiError(res.error || 'No se pudo añadir el servidor MCP.', 200, res);
  }
  return res;
}

export function removeMcpServer(serverId) {
  return request(`/mcp/${encodeURIComponent(serverId)}`, { method: 'DELETE' });
}

/** Search the official MCP registry (registry.modelcontextprotocol.io). */
export function searchMcpRegistry(query, limit = 30) {
  // External registry over the VM's egress — give it more room than a local GET,
  // but still bounded so the search box never hangs forever.
  return request(`/mcp/registry?q=${encodeURIComponent(query)}&limit=${limit}`, { timeoutMs: 25000 }).catch(_emptyOnNotFound([]));
}

// ── Web search backend keys (Brave/Tavily/Exa) ─────────────────────────────────
export function getWebSearchStatus() {
  return request('/web-search/status').catch(() => ({ brave: false, ddgs_fallback: true }));
}
export function setWebSearchKey(provider, apiKey) {
  return request('/web-search/key', { method: 'POST', body: JSON.stringify({ provider, api_key: apiKey }) });
}

// ── Security ─────────────────────────────────────────────────────────────────

export function getSecurityScans() {
  return request('/security/scans').catch(_emptyOnNotFound([]));
}

export function getAuditChainHead() {
  return request('/security/audit/head').catch(() => null);
}

export function getSecurityPolicy() {
  return request('/security/policy').catch(() => null);
}

/** Sovereign owner override: allow an install the scanner flagged (FAIL/WARN). MFA-gated. */
export function recordInstallDecision(payload) {
  return request('/security/decisions', { method: 'POST', body: JSON.stringify(payload), timeoutMs: 30000 });
}

// ── Egress permissions (owner-elevated allow-list) ───────────────────────────
export function listEgressDomains() {
  return request('/egress/domains').catch(_emptyOnNotFound({ domains: [] }));
}

export function grantEgressDomain(domain) {
  return request('/egress/domains/grant', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  });
}

export function revokeEgressDomain(domain) {
  return request('/egress/domains/revoke', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  });
}

// ── Memory ───────────────────────────────────────────────────────────────────

export function listMemory() {
  return request('/memory').catch(_emptyOnNotFound([]));
}

export function searchMemory(query) {
  return request(`/memory/search?q=${encodeURIComponent(query)}`).catch(_emptyOnNotFound([]));
}

// ── Workspace files ───────────────────────────────────────────────────────────

export function listWorkspaceFiles() {
  return request('/workspace/files').catch(_emptyOnNotFound([]));
}

// ── Approvals (HITL) ─────────────────────────────────────────────────────────

export function listPendingApprovals() {
  return request('/approvals/pending').catch(_emptyOnNotFound([]));
}

export function resolveApproval(proposalId, decision, factors = {}) {
  // Approving forwards the owner's MFA factors to the gate (the single enforcement
  // point); the gate derives the required tier server-side. Denying needs no factors.
  return request(`/approvals/${encodeURIComponent(proposalId)}`, {
    method: 'POST',
    body: JSON.stringify({
      decision,
      totp: factors.totp ?? null,
      humanity: factors.humanity ?? null,
      riddle_answer: factors.riddle_answer ?? null,
    }),
  });
}

// ── MFA enrollment (owner) ─────────────────────────────────────────────────────
export function mfaStatus() {
  return request('/mfa/status').catch(_emptyOnNotFound({ enrolled: false, riddle_set: false }));
}
export function mfaEnroll(totp = null) {
  return request('/mfa/enroll', { method: 'POST', body: JSON.stringify({ totp }) });
}
export function mfaSetRiddle(totp, question, answer) {
  return request('/mfa/riddle', { method: 'POST', body: JSON.stringify({ totp, question, answer }) });
}

// ── Security policies (per-command + presets + MFA-on-dangers) ──────────────────
export function getPolicies() {
  return request('/policies').catch(_emptyOnNotFound({ preset: 'equilibrado', tools: {}, mfa_on_dangers: true }));
}
export function setPolicyPreset(preset, totp, riddle_answer = null) {
  return request('/policies/preset', { method: 'POST', body: JSON.stringify({ preset, totp, riddle_answer }) });
}
export function setPolicyTool(tool, enabled, totp, riddle_answer = null) {
  return request('/policies/tool', { method: 'POST', body: JSON.stringify({ tool, enabled, totp, riddle_answer }) });
}
export function setMfaOnDangers(enabled, totp, riddle_answer = null) {
  return request('/policies/mfa_on_dangers', { method: 'POST', body: JSON.stringify({ enabled, totp, riddle_answer }) });
}

// ── Runtime / Profile ─────────────────────────────────────────────────────────

export function getRuntimeStatus() {
  return request('/runtime/status').catch(() => ({ state: 'unknown', active_task_count: 0 }));
}

export function getProfile() {
  return fetch('/api/v1/profile').then(r => r.json()).catch(() => ({ profile: 'unknown', user: 'Luis' }));
}

// ── Internal helpers ──────────────────────────────────────────────────────────

function _emptyOnNotFound(fallback) {
  return () => fallback;
}
