/**
 * useChat — manages a single chat conversation with live WebSocket streaming.
 *
 * Mirrors the logic in vanilla chat.js (sendMessage / attachLiveStream) but
 * expressed as a React hook with immutable state instead of direct DOM mutation.
 */

import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { postChat, openTaskStream, getConversation } from '../api/client'
import type { StreamCallbacks } from '../api/client'
import type { StreamFrame } from '../api/types'
import { renderMarkdown } from '../lib/markdown'

const SS_CONV_ID = 'lumen:convId'
const SS_TASK_ID = 'lumen:taskId'

function genUUID(): string {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  } catch { /* not secure context */ }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
  })
}

export interface ToolStep {
  name: string
  label: string
  target: string
}

// Each message in the rendered list is one of these shapes.
export type ChatMessage =
  | { type: 'user'; id: string; text: string }
  | {
      type: 'assistant'
      id: string
      thinkingText: string
      thinkingDone: boolean
      toolSteps: ToolStep[]
      /** Raw streaming activity text (replaced by rendered HTML on done). */
      activityText: string
      /** Final rendered HTML (set on stream done). Empty while streaming. */
      renderedHtml: string
      isStreaming: boolean
    }

type ChatStatus =
  | { phase: 'idle' }
  | { phase: 'sending' }
  | { phase: 'streaming'; statusText: string }
  | { phase: 'error'; message: string }

interface ChatState {
  convId: string | null
  messages: ChatMessage[]
  status: ChatStatus
}

type Action =
  | { type: 'RESET' }
  | { type: 'ADD_USER'; id: string; text: string }
  | { type: 'ADD_ASSISTANT'; id: string }
  | { type: 'STATUS_SENDING' }
  | { type: 'STATUS_STREAMING'; text: string }
  | { type: 'STATUS_IDLE' }
  | { type: 'STATUS_ERROR'; message: string }
  | { type: 'SET_CONV_ID'; convId: string }
  | { type: 'DELTA'; id: string; chunk: string }
  | { type: 'THINKING'; id: string; chunk: string }
  | { type: 'THINKING_DONE'; id: string }
  | { type: 'TOOL_CALL'; id: string; step: ToolStep }
  | { type: 'STREAM_DONE'; id: string }
  | { type: 'LOAD_MESSAGES'; convId: string; messages: ChatMessage[] }

function updateAssistant(
  messages: ChatMessage[],
  id: string,
  updater: (m: Extract<ChatMessage, { type: 'assistant' }>) => Extract<ChatMessage, { type: 'assistant' }>,
): ChatMessage[] {
  return messages.map(m => (m.type === 'assistant' && m.id === id ? updater(m) : m))
}

function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case 'RESET':
      return { convId: null, messages: [], status: { phase: 'idle' } }

    case 'SET_CONV_ID':
      return { ...state, convId: action.convId }

    case 'ADD_USER':
      return {
        ...state,
        messages: [
          ...state.messages,
          { type: 'user', id: action.id, text: action.text },
        ],
      }

    case 'ADD_ASSISTANT':
      return {
        ...state,
        messages: [
          ...state.messages,
          {
            type: 'assistant',
            id: action.id,
            thinkingText: '',
            thinkingDone: false,
            toolSteps: [],
            activityText: '',
            renderedHtml: '',
            isStreaming: true,
          },
        ],
      }

    case 'STATUS_SENDING':
      return { ...state, status: { phase: 'sending' } }

    case 'STATUS_STREAMING':
      return { ...state, status: { phase: 'streaming', statusText: action.text } }

    case 'STATUS_IDLE':
      return { ...state, status: { phase: 'idle' } }

    case 'STATUS_ERROR':
      return { ...state, status: { phase: 'error', message: action.message } }

    case 'DELTA':
      return {
        ...state,
        messages: updateAssistant(state.messages, action.id, m => ({
          ...m,
          activityText: m.activityText + action.chunk,
        })),
      }

    case 'THINKING':
      return {
        ...state,
        messages: updateAssistant(state.messages, action.id, m => ({
          ...m,
          thinkingText: m.thinkingText + action.chunk,
        })),
      }

    case 'THINKING_DONE':
      return {
        ...state,
        messages: updateAssistant(state.messages, action.id, m => ({
          ...m,
          thinkingDone: true,
        })),
      }

    case 'TOOL_CALL':
      return {
        ...state,
        messages: updateAssistant(state.messages, action.id, m => ({
          ...m,
          toolSteps: [...m.toolSteps, action.step],
          // Reset the activity segment on each tool call (matches vanilla segmentStart logic)
          activityText: '',
        })),
      }

    case 'STREAM_DONE':
      return {
        ...state,
        messages: updateAssistant(state.messages, action.id, m => (m.isStreaming ? {
          ...m,
          isStreaming: false,
          renderedHtml: renderMarkdown(m.activityText.trim() || m.activityText),
          activityText: '',
          thinkingDone: true,
        } : m)),
        status: { phase: 'idle' },
      }

    case 'LOAD_MESSAGES':
      return { ...state, convId: action.convId, messages: action.messages }

    default:
      return state
  }
}

interface UseChatReturn {
  convId: string | null
  messages: ChatMessage[]
  status: ChatStatus
  /** True while re-attaching to a stream that was in-flight before a page refresh. */
  reconnecting: boolean
  sendMessage(text: string): Promise<void>
  startNew(): void
  stopStream(): void
  loadConversation(id: string): Promise<void>
}

export function useChat(): UseChatReturn {
  const [state, dispatch] = useReducer(reducer, {
    convId: null,
    messages: [],
    status: { phase: 'idle' },
  })

  const streamRef = useRef<{ close(): void } | null>(null)
  // Stable ref to the assistant message id currently streaming
  const activeAssistantIdRef = useRef<string | null>(null)
  // Tracks whether the mount-restore has already run
  const restoredRef = useRef(false)
  // "reconectando…" status while re-attaching a stream after refresh
  const [reconnecting, setReconnecting] = useState(false)

  const stopStream = useCallback(() => {
    streamRef.current?.close()
    streamRef.current = null
    // Freeze any in-flight assistant message so partial text is preserved and
    // the spinner does not hang. STATUS_IDLE is included in STREAM_DONE's reducer.
    const activeId = activeAssistantIdRef.current
    if (activeId) {
      dispatch({ type: 'STREAM_DONE', id: activeId })
      activeAssistantIdRef.current = null
    } else {
      dispatch({ type: 'STATUS_IDLE' })
    }
    sessionStorage.removeItem(SS_TASK_ID)
  }, [])

  const startNew = useCallback(() => {
    stopStream()
    sessionStorage.removeItem(SS_CONV_ID)
    sessionStorage.removeItem(SS_TASK_ID)
    dispatch({ type: 'RESET' })
  }, [stopStream])

  // Persist convId to sessionStorage whenever it changes so a refresh can restore it.
  useEffect(() => {
    if (state.convId) {
      sessionStorage.setItem(SS_CONV_ID, state.convId)
    }
  }, [state.convId])

  // On mount: restore the last conversation and, if a task was streaming, re-attach.
  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true

    const savedConvId = sessionStorage.getItem(SS_CONV_ID)
    const savedTaskId = sessionStorage.getItem(SS_TASK_ID)

    if (!savedConvId) return

    // Load the conversation history first.
    getConversation(savedConvId)
      .then(detail => {
        const messages: ChatMessage[] = detail.messages
          .filter(m => m.role === 'user' || m.role === 'assistant')
          .map(m => {
            if (m.role === 'user') {
              return { type: 'user' as const, id: genUUID(), text: m.content }
            }
            return {
              type: 'assistant' as const,
              id: genUUID(),
              thinkingText: '',
              thinkingDone: true,
              toolSteps: [],
              activityText: '',
              renderedHtml: renderMarkdown(m.content),
              isStreaming: false,
            }
          })
        dispatch({ type: 'LOAD_MESSAGES', convId: savedConvId, messages })

        // If a task stream was in-flight, re-attach to receive the rest.
        if (savedTaskId) {
          const assistantMsgId = genUUID()
          dispatch({ type: 'ADD_ASSISTANT', id: assistantMsgId })
          dispatch({ type: 'STATUS_STREAMING', text: 'Reconectando…' })
          activeAssistantIdRef.current = assistantMsgId
          setReconnecting(true)

          // The FIRST frame of any kind proves the re-attach succeeded and the stream
          // is live again — so we're no longer "reconnecting". Clearing only on done/error
          // (below) left "Reconectando…" stuck for the whole task even while frames flowed.
          const markConnected = () => setReconnecting(false)
          const callbacks: StreamCallbacks = {
            onDelta(chunk) { markConnected(); dispatch({ type: 'DELTA', id: assistantMsgId, chunk }) },
            onThinking(chunk) { markConnected(); dispatch({ type: 'THINKING', id: assistantMsgId, chunk }) },
            onToolCall(frame: Extract<StreamFrame, { kind: 'tool_call' }>) {
              markConnected()
              const d = frame.tool_call ?? (frame as Record<string, unknown>)
              const name = (d.tool as string | undefined) ?? (d.tool_name as string | undefined) ?? 'herramienta'
              const label = (d.label as string | undefined) ?? String(name).replace(/_/g, ' ')
              const target = String((d.target as string | undefined) ?? '').slice(0, 80)
              dispatch({ type: 'TOOL_CALL', id: assistantMsgId, step: { name, label, target } })
              dispatch({ type: 'THINKING_DONE', id: assistantMsgId })
            },
            onStatus(msg) { markConnected(); dispatch({ type: 'STATUS_STREAMING', text: msg }) },
            onDone() {
              dispatch({ type: 'STREAM_DONE', id: assistantMsgId })
              streamRef.current = null
              activeAssistantIdRef.current = null
              sessionStorage.removeItem(SS_TASK_ID)
              setReconnecting(false)
            },
            onError(msg) {
              dispatch({ type: 'STATUS_ERROR', message: msg })
              streamRef.current = null
              activeAssistantIdRef.current = null
              sessionStorage.removeItem(SS_TASK_ID)
              setReconnecting(false)
            },
          }
          streamRef.current = openTaskStream(savedTaskId, callbacks)
        }
      })
      .catch(() => {
        // Stale session — clear and start fresh.
        sessionStorage.removeItem(SS_CONV_ID)
        sessionStorage.removeItem(SS_TASK_ID)
      })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim()) return

    stopStream()

    // Own the conversation id client-side (mirrors vanilla chat.js genUUID pattern)
    let convId = state.convId
    if (!convId) {
      convId = genUUID()
      dispatch({ type: 'SET_CONV_ID', convId })
    }

    const userMsgId = genUUID()
    const assistantMsgId = genUUID()

    dispatch({ type: 'ADD_USER', id: userMsgId, text })
    dispatch({ type: 'STATUS_SENDING' })

    let taskId: string
    try {
      const res = await postChat({
        conversation_id: convId,
        user_message: text,
        dedup_key: `chat:${Date.now()}:${Math.random().toString(36).slice(2)}`,
      })
      taskId = res.task_id
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Error al enviar'
      dispatch({ type: 'STATUS_ERROR', message: msg })
      return
    }

    dispatch({ type: 'ADD_ASSISTANT', id: assistantMsgId })
    dispatch({ type: 'STATUS_STREAMING', text: 'Procesando…' })
    activeAssistantIdRef.current = assistantMsgId
    sessionStorage.setItem(SS_TASK_ID, taskId)

    const callbacks: StreamCallbacks = {
      onDelta(chunk) {
        dispatch({ type: 'DELTA', id: assistantMsgId, chunk })
      },
      onThinking(chunk) {
        dispatch({ type: 'THINKING', id: assistantMsgId, chunk })
      },
      onToolCall(frame: Extract<StreamFrame, { kind: 'tool_call' }>) {
        // d can be the nested descriptor OR the frame itself — both shapes share tool/label/target.
        const d = frame.tool_call ?? (frame as Record<string, unknown>)
        const name = (d.tool as string | undefined) ?? (d.tool_name as string | undefined) ?? 'herramienta'
        const label = (d.label as string | undefined) ?? String(name).replace(/_/g, ' ')
        const target = String((d.target as string | undefined) ?? '').slice(0, 80)
        dispatch({
          type: 'TOOL_CALL',
          id: assistantMsgId,
          step: { name, label, target },
        })
        dispatch({ type: 'THINKING_DONE', id: assistantMsgId })
      },
      onStatus(msg) {
        dispatch({ type: 'STATUS_STREAMING', text: msg })
      },
      onDone() {
        dispatch({ type: 'STREAM_DONE', id: assistantMsgId })
        streamRef.current = null
        activeAssistantIdRef.current = null
        sessionStorage.removeItem(SS_TASK_ID)
      },
      onError(msg) {
        dispatch({ type: 'STATUS_ERROR', message: msg })
        streamRef.current = null
        activeAssistantIdRef.current = null
        sessionStorage.removeItem(SS_TASK_ID)
      },
    }

    streamRef.current = openTaskStream(taskId, callbacks)
  }, [state.convId, stopStream])

  const loadConversation = useCallback(async (id: string) => {
    stopStream()
    try {
      const detail = await getConversation(id)
      const messages: ChatMessage[] = detail.messages
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .map(m => {
          if (m.role === 'user') {
            return { type: 'user' as const, id: genUUID(), text: m.content }
          }
          return {
            type: 'assistant' as const,
            id: genUUID(),
            thinkingText: '',
            thinkingDone: true,
            toolSteps: [],
            activityText: '',
            renderedHtml: renderMarkdown(m.content),
            isStreaming: false,
          }
        })
      dispatch({ type: 'LOAD_MESSAGES', convId: id, messages })
    } catch {
      dispatch({ type: 'STATUS_ERROR', message: 'No se pudo cargar la conversación.' })
    }
  }, [stopStream])

  return {
    convId: state.convId,
    messages: state.messages,
    status: state.status,
    reconnecting,
    sendMessage,
    startNew,
    stopStream,
    loadConversation,
  }
}
