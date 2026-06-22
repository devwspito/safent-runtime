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

// Frames emitted by the WebSocket stream — discriminated by `kind`.
export type StreamFrame =
  | { kind: 'delta';          delta?: string; text?: string }
  | { kind: 'thinking_delta'; thinking?: string; delta?: string; text?: string }
  | { kind: 'tool_call';      tool_call?: ToolCallDescriptor; tool?: string; label?: string; target?: string }
  | { kind: 'status';         message?: string; status?: string }
  | { kind: 'done' }
  | { kind: 'error';          message?: string }
