/**
 * useChat — manages a single chat conversation with live WebSocket streaming.
 *
 * Mirrors the logic in vanilla chat.js (sendMessage / attachLiveStream) but
 * expressed as a React hook with immutable state instead of direct DOM mutation.
 *
 * Resilience strategy (belt-and-suspenders, same model as Office/Live):
 *   - The WS stream is the fast path — frame-by-frame deltas while it flows.
 *   - A 2 s polling safety-net runs whenever an assistant turn is in-flight:
 *       (a) getRuntimeStatus() → drives status text from activity[].tool so the UI
 *           shows life during long silent tool calls; clears "Reconectando…".
 *       (b) getConversation()  → detects when the daemon has written the final
 *           assistant turn to its conversation mirror; adopts it (ADOPT_FINAL) if
 *           the WS hasn't already produced a finalized renderedHtml. This guarantees
 *           the answer ALWAYS appears even if the WS missed the `done` frame.
 *   The poll is cleared on: STREAM_DONE, ADOPT_FINAL, stopStream, startNew, unmount.
 */

import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { postChat, openTaskStream, getConversation, getRuntimeStatus } from '../api/client'
import type { StreamCallbacks } from '../api/client'
import type { StreamFrame } from '../api/types'
import { renderMarkdown } from '../lib/markdown'
import { toolLabel } from '../lib/toolLabels'

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
  /**
   * ADOPT_FINAL: the polling safety-net detected that the daemon wrote the final
   * assistant turn to its conversation mirror, but the WS `done` frame was missed
   * (e.g. a page refresh happened while the task was running). We inject the
   * pre-rendered HTML into the in-flight assistant bubble and seal it.
   *
   * Guard: only applied when `isStreaming === true` for that message so we never
   * overwrite a WS-finalized turn that arrived later on the same render cycle.
   */
  | { type: 'ADOPT_FINAL'; id: string; renderedHtml: string }

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

    case 'ADOPT_FINAL':
      return {
        ...state,
        // Only seal the bubble if it is still streaming — if the WS already
        // produced a STREAM_DONE on the same tick, isStreaming is already false
        // and we leave that renderedHtml untouched (no double-render).
        messages: updateAssistant(state.messages, action.id, m => m.isStreaming ? {
          ...m,
          isStreaming: false,
          renderedHtml: action.renderedHtml,
          activityText: '',
          thinkingDone: true,
        } : m),
        status: { phase: 'idle' },
      }

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

  // The task_id for the in-flight assistant turn. Set before startPoll, cleared
  // when the turn finalises. The poll reads this to scope the activity indicator
  // to THIS conversation's task — not another agent's concurrent task.
  const currentTaskIdRef = useRef<string | null>(null)

  // ── Polling safety-net interval ref ──────────────────────────────────────────
  // Cleared whenever a turn finalises (STREAM_DONE / ADOPT_FINAL / stopStream /
  // startNew / unmount). Started whenever an assistant turn is in-flight.
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Stable ref to the baseline finalized-assistant-message count captured when a
  // poll is started. Using a ref avoids the stale-closure problem that would occur
  // if we captured state.messages inside startPoll's useCallback deps array.
  const baselineAssistantCountRef = useRef(0)

  const clearPoll = useCallback(() => {
    if (pollIntervalRef.current !== null) {
      clearInterval(pollIntervalRef.current)
      pollIntervalRef.current = null
    }
  }, [])

  /**
   * Start the 2 s polling safety-net for the given assistant message and convId.
   * Each tick does two things:
   *   1. getRuntimeStatus() → drives status text from live tool activity
   *      so the UI shows life during long silent WS gaps (e.g. browser_navigate).
   *   2. getConversation()  → detects whether the daemon wrote the final turn to
   *      its mirror while the WS was silent/dead; adopts it via ADOPT_FINAL.
   *
   * The poll does NOT replace the WS — it is a belt-and-suspenders fallback.
   * While WS deltas are flowing, the poll's ADOPT_FINAL guard (isStreaming check
   * inside the reducer) prevents double-rendering.
   *
   * baselineCount must be passed in by the caller, computed from the message list
   * at the moment the new assistant turn begins (before ADD_ASSISTANT is dispatched),
   * so we correctly detect when a NEW assistant message appears in the mirror.
   */
  const startPoll = useCallback((convId: string, baselineCount: number) => {
    clearPoll()
    baselineAssistantCountRef.current = baselineCount

    pollIntervalRef.current = setInterval(() => {
      const currentAssistantId = activeAssistantIdRef.current
      // If the stream finished via the WS path, stop polling.
      if (currentAssistantId === null) {
        clearPoll()
        return
      }

      // (1) Live tool indicator — scoped to THIS conversation's task_id.
      //     currentTaskIdRef.current is set by the caller before startPoll so the
      //     closure always sees the correct value via the ref (no stale capture).
      void getRuntimeStatus()
        .then(runtimeStatus => {
          if (activeAssistantIdRef.current !== currentAssistantId) return

          if ((runtimeStatus.active_task_count ?? 0) > 0) {
            const thisTaskId = currentTaskIdRef.current
            const activities = runtimeStatus.activity ?? []

            // Only use an entry that belongs to THIS conversation's task.
            // If task_id is absent (older backend), fall back to generic text —
            // never show another conversation's tool name here.
            const entry = thisTaskId
              ? activities.find(a => a.task_id === thisTaskId)
              : undefined

            if (entry?.tool) {
              const humanized = toolLabel(entry.tool)
              if (humanized) {
                dispatch({ type: 'STATUS_STREAMING', text: `Trabajando… (${humanized.toLowerCase()})` })
                setReconnecting(false)
                return
              }
            }
            // Task is running but no matching entry or no humanizable tool name —
            // show a generic pulse without leaking another task's tool.
            dispatch({ type: 'STATUS_STREAMING', text: 'Trabajando…' })
            setReconnecting(false)
          }
        })
        .catch(() => { /* transient — keep last text */ })

      // (2) Final-answer guarantee — adopt the mirror answer if the WS missed done
      void getConversation(convId)
        .then(detail => {
          if (activeAssistantIdRef.current !== currentAssistantId) return

          const mirrorMessages = (detail.messages ?? []).filter(
            m => m.role === 'assistant',
          )
          // A new finalized assistant message has appeared in the mirror beyond baseline
          if (mirrorMessages.length > baselineAssistantCountRef.current) {
            // Pick the last one (chronological order not guaranteed, but last is
            // the most recent reply). Fall back to empty string if content missing.
            const last = mirrorMessages[mirrorMessages.length - 1]
            const content = last?.content ?? ''

            // ADOPT_FINAL has an isStreaming guard in the reducer — if the WS already
            // produced STREAM_DONE for this turn, the action is a no-op (no double-render).
            dispatch({
              type: 'ADOPT_FINAL',
              id: currentAssistantId,
              renderedHtml: renderMarkdown(content.trim()),
            })
            streamRef.current?.close()
            streamRef.current = null
            activeAssistantIdRef.current = null
            currentTaskIdRef.current = null
            sessionStorage.removeItem(SS_TASK_ID)
            setReconnecting(false)
            clearPoll()
          }
        })
        .catch(() => { /* stale convId or transient error — keep polling */ })
    }, 2_000)
  }, [clearPoll])

  const stopStream = useCallback(() => {
    clearPoll()
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
    currentTaskIdRef.current = null
    sessionStorage.removeItem(SS_TASK_ID)
    setReconnecting(false)
  }, [clearPoll])

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
  // Also starts the polling safety-net so that long silent tool calls never freeze
  // the UI and the final answer is always rendered even if the WS done frame was missed.
  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true

    const savedConvId = sessionStorage.getItem(SS_CONV_ID)
    const savedTaskId = sessionStorage.getItem(SS_TASK_ID)

    if (!savedConvId) return

    // Load the conversation history first.
    getConversation(savedConvId)
      .then(detail => {
        const messages: ChatMessage[] = (detail.messages ?? [])
          .filter(m => m.role === 'user' || m.role === 'assistant')
          .map(m => {
            if (m.role === 'user') {
              return { type: 'user' as const, id: genUUID(), text: m.content ?? '' }
            }
            return {
              type: 'assistant' as const,
              id: genUUID(),
              thinkingText: '',
              thinkingDone: true,
              toolSteps: [],
              activityText: '',
              renderedHtml: renderMarkdown(m.content ?? ''),
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
          currentTaskIdRef.current = savedTaskId
          setReconnecting(true)

          // Start the polling safety-net immediately after re-attachment.
          // Baseline = all assistant messages already in history (all finalized).
          // It will (a) drive live tool labels from runtime/status so "Reconectando…"
          // clears within one poll tick, and (b) detect the final answer in the mirror
          // if the WS done frame was already missed before we got here.
          const baselineCount = messages.filter(m => m.type === 'assistant').length
          startPoll(savedConvId, baselineCount)

          // The FIRST frame of any kind proves the re-attach succeeded and the stream
          // is live again — so we're no longer "reconnecting". Clearing only on done/error
          // (below) left "Reconectando…" stuck for the whole task even while frames flowed.
          const markConnected = () => setReconnecting(false)
          const callbacks: StreamCallbacks = {
            onDelta(chunk) {
              markConnected()
              dispatch({ type: 'DELTA', id: assistantMsgId, chunk })
            },
            onThinking(chunk) {
              markConnected()
              dispatch({ type: 'THINKING', id: assistantMsgId, chunk })
            },
            onToolCall(frame: Extract<StreamFrame, { kind: 'tool_call' }>) {
              markConnected()
              const d = frame.tool_call ?? (frame as Record<string, unknown>)
              const name = (d.tool as string | undefined) ?? (d.tool_name as string | undefined) ?? 'herramienta'
              const label = (d.label as string | undefined) ?? String(name).replace(/_/g, ' ')
              const target = String((d.target as string | undefined) ?? '').slice(0, 80)
              dispatch({ type: 'TOOL_CALL', id: assistantMsgId, step: { name, label, target } })
              dispatch({ type: 'THINKING_DONE', id: assistantMsgId })
            },
            onStatus(msg) {
              markConnected()
              dispatch({ type: 'STATUS_STREAMING', text: msg })
            },
            onDone() {
              clearPoll()
              dispatch({ type: 'STREAM_DONE', id: assistantMsgId })
              streamRef.current = null
              activeAssistantIdRef.current = null
              currentTaskIdRef.current = null
              sessionStorage.removeItem(SS_TASK_ID)
              setReconnecting(false)
            },
            onError(_msg) {
              // On WS error after re-attach, keep the poll running — the task may
              // still be running and the mirror will eventually have the answer.
              // Show a neutral "working" status rather than an error bar so the user
              // is not alarmed while the poll continues watching for the final answer.
              dispatch({ type: 'STATUS_STREAMING', text: 'Trabajando…' })
              streamRef.current = null
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

  // Clear the poll on unmount so intervals never leak between route navigations.
  useEffect(() => {
    return () => { clearPoll() }
  }, [clearPoll])

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
    currentTaskIdRef.current = taskId
    sessionStorage.setItem(SS_TASK_ID, taskId)

    // Start the safety-net poll. Baseline = finalized assistant messages already
    // in state BEFORE we added the new in-flight bubble (ADD_ASSISTANT not yet
    // reflected in state.messages at this point in the async callback chain, but
    // we computed it from the snapshot at the top of sendMessage which is fine).
    // convId is guaranteed non-null here (set above before postChat).
    const baselineSend = state.messages.filter(
      m => m.type === 'assistant' && !m.isStreaming,
    ).length
    startPoll(convId!, baselineSend)

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
        clearPoll()
        dispatch({ type: 'STREAM_DONE', id: assistantMsgId })
        streamRef.current = null
        activeAssistantIdRef.current = null
        currentTaskIdRef.current = null
        sessionStorage.removeItem(SS_TASK_ID)
      },
      onError(msg) {
        // Keep the poll running on WS error — the task may still be running and
        // the mirror poll will surface the final answer when it appears.
        dispatch({ type: 'STATUS_ERROR', message: msg })
        streamRef.current = null
        sessionStorage.removeItem(SS_TASK_ID)
      },
    }

    streamRef.current = openTaskStream(taskId, callbacks)
  }, [state.convId, stopStream, startPoll, clearPoll])

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
