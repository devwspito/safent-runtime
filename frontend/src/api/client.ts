import { token } from '../lib/token'
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

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
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

export function createAgent(payload: CreateAgentPayload): Promise<Agent> {
  return request<Agent>('/agents', { method: 'POST', body: JSON.stringify(payload) })
}

export function listMcpServers(): Promise<Array<{ id: string; slug: string; name: string }>> {
  return request<Array<{ id: string; slug: string; name: string }>>('/mcp/servers').catch(() => [])
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
  return request<ConversationSummary[]>(`/chat/conversations${qs}`).catch(() => [])
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
