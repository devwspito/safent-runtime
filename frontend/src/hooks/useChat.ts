/**
 * useChat — manages a single chat conversation with live SSE streaming.
 *
 * Implements the sendMessage / live-stream-attach flow as a React hook with
 * immutable state (instead of direct DOM mutation), resuming via Last-Event-ID.
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

/** Maximum chars kept in live thinkingText / activityText during streaming.
 *  The tail is sufficient for display; the final complete answer lives in renderedHtml. */
const LIVE_TEXT_CAP = 8_000

/** Flush interval in ms — limits React re-renders to ~8/sec regardless of frame rate. */
const FLUSH_INTERVAL_MS = 120

const SS_CONV_ID = 'safent:convId'
const SS_TASK_ID = 'safent:taskId'
const SS_AGENT_ID = 'safent:agentId'

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
      /** Backend task_id that produced this turn; null for restored history before the new contract. */
      taskId: string | null
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
  /** Agent bound to this conversation (set on first message; immutable after). */
  agentId: string | null
  messages: ChatMessage[]
  status: ChatStatus
}

type Action =
  | { type: 'RESET' }
  | { type: 'ADD_USER'; id: string; text: string }
  | { type: 'ADD_ASSISTANT'; id: string; taskId: string }
  | { type: 'STATUS_SENDING' }
  | { type: 'STATUS_STREAMING'; text: string }
  | { type: 'STATUS_IDLE' }
  | { type: 'STATUS_ERROR'; message: string }
  | { type: 'SET_CONV_ID'; convId: string }
  | { type: 'SET_AGENT_ID'; agentId: string }
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
  /**
   * BATCH_UPDATE: coalesced flush from the throttle buffer.
   * Applies accumulated thinking chunks, delta chunks, new tool steps, and the
   * latest status string all in a single state update — one re-render per flush
   * instead of one per WS frame.
   */
  | {
      type: 'BATCH_UPDATE'
      id: string
      thinkingChunk: string
      deltaChunk: string
      newToolSteps: ToolStep[]
      thinkingDone: boolean
      statusText: string | null
    }

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
      return { convId: null, agentId: null, messages: [], status: { phase: 'idle' } }

    case 'SET_CONV_ID':
      return { ...state, convId: action.convId }

    case 'SET_AGENT_ID':
      return { ...state, agentId: action.agentId }

    case 'ADD_USER':
      return {
        ...state,
        messages: [
          ...state.messages,
          { type: 'user', id: action.id, text: action.text },
        ],
      }

    case 'ADD_ASSISTANT': {
      // Idempotent: if a bubble with this taskId already exists (e.g. from a
      // previous LOAD_MESSAGES that carried task_id from the mirror), reuse it
      // rather than appending a duplicate. The caller anchors activeAssistantIdRef
      // to the existing bubble's id in that case.
      const existing = state.messages.find(
        m => m.type === 'assistant' && m.taskId === action.taskId,
      )
      if (existing) return state

      return {
        ...state,
        messages: [
          ...state.messages,
          {
            type: 'assistant',
            id: action.id,
            taskId: action.taskId,
            thinkingText: '',
            thinkingDone: false,
            toolSteps: [],
            activityText: '',
            renderedHtml: '',
            isStreaming: true,
          },
        ],
      }
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
          // activityText is NOT capped — the full text is needed for renderMarkdown on STREAM_DONE
          // AND the live display now renders the full growing text (scrollable box), not just
          // the last line.
          activityText: m.activityText + action.chunk,
        })),
      }

    case 'THINKING':
      return {
        ...state,
        messages: updateAssistant(state.messages, action.id, m => ({
          ...m,
          thinkingText: (m.thinkingText + action.chunk).slice(-LIVE_TEXT_CAP),
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
          // Do NOT reset activityText: a tool call mid-answer must not discard the
          // answer text streamed before it. Keeping it accumulating means STREAM_DONE
          // renders the WHOLE multi-segment answer, not just the fragment after the
          // last tool call (that dropped table rows / stranded a "¿?" tail bubble).
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
      return { ...state, convId: action.convId, agentId: state.agentId, messages: action.messages }

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

    case 'BATCH_UPDATE': {
      // Single immutable update covering all accumulated WS frames from one flush tick.
      // activityText accumulates across tool calls (never reset) so the full answer survives.
      const { id, thinkingChunk, deltaChunk, newToolSteps, thinkingDone, statusText } = action
      const nextStatus: ChatState['status'] = statusText !== null
        ? { phase: 'streaming', statusText }
        : state.status

      if (!thinkingChunk && !deltaChunk && newToolSteps.length === 0 && !thinkingDone) {
        // Only a status update — avoid touching messages array.
        return { ...state, status: nextStatus }
      }

      return {
        ...state,
        status: nextStatus,
        messages: updateAssistant(state.messages, id, m => {
          let next = m
          if (newToolSteps.length > 0) {
            // Do NOT reset activityText (see TOOL_CALL): earlier answer segments must
            // survive so the finalized bubble is the full answer, not just the tail.
            next = { ...next, toolSteps: [...next.toolSteps, ...newToolSteps] }
          }
          if (thinkingChunk) {
            // thinkingText is capped — it's a transient trace only shown in a collapsed
            // <details> block. The final answer is never derived from thinkingText.
            next = { ...next, thinkingText: (next.thinkingText + thinkingChunk).slice(-LIVE_TEXT_CAP) }
          }
          if (deltaChunk) {
            // activityText is NOT capped — the full text is needed for renderMarkdown on STREAM_DONE
            // AND the live display now renders the full growing text (scrollable box), not just
            // the last line.
            next = { ...next, activityText: next.activityText + deltaChunk }
          }
          if (thinkingDone && !next.thinkingDone) {
            next = { ...next, thinkingDone: true }
          }
          return next
        }),
      }
    }

    default:
      return state
  }
}

interface UseChatReturn {
  convId: string | null
  /** Agent bound to the current conversation (null = CEO / default). */
  agentId: string | null
  messages: ChatMessage[]
  status: ChatStatus
  /** True while re-attaching to a stream that was in-flight before a page refresh. */
  reconnecting: boolean
  /** Sticky: the in-flight turn's task is using the browser → chat can show live view. */
  liveBrowserActive: boolean
  /** Bumped after a turn finishes (conversation persisted) → RecentsSection refetches. */
  conversationsTick: number
  sendMessage(text: string): Promise<void>
  startNew(): void
  /** Start a new conversation pre-bound to a specific agent. */
  startNewWithAgent(agentId: string): void
  stopStream(): void
  loadConversation(id: string): Promise<void>
}

export function useChat(): UseChatReturn {
  const [state, dispatch] = useReducer(reducer, {
    convId: null,
    agentId: null,
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

  // The "usando el navegador / Ver en vivo" chip. It is driven by the REAL jailed-
  // browser state (runtime_status.browser_live) AND-ed with a per-CONVERSATION marker
  // (browserUsedRef) that this conversation actually issued a browser_* tool. The AND
  // matters: browser_live is a GLOBAL probe of the single jailed Chromium, so a
  // conversation that never touched the browser must NOT light its chip just because
  // another conversation left a page open. A failed browser_navigate / a web_search
  // never opens a real page → browser_live=false → no chip.
  const [liveBrowserActive, setLiveBrowserActive] = useState(false)
  // Bumped after a turn FINISHES (onDone / ADOPT_FINAL) — i.e. AFTER the daemon has
  // persisted the conversation to the mirror — so the sidebar re-fetches and a brand-new
  // chat appears without a full reload. (The list's only other refresh trigger fires on
  // the client-side convId change, which happens pre-persist and always misses the row.)
  const [conversationsTick, setConversationsTick] = useState(0)
  // Per-conversation: true once THIS conversation issued a browser_* tool call. Reset
  // on conversation switch (startNew / loadConversation). Necessary-but-not-sufficient
  // for the chip — browser_live provides the "there is a real live page" sufficiency.
  const browserUsedRef = useRef(false)

  // ── Coalesce / throttle buffer ────────────────────────────────────────────────
  // Instead of dispatching on every WS frame we accumulate here and flush at most
  // once per FLUSH_INTERVAL_MS. This limits React re-renders to ~8/sec regardless
  // of how fast the backend streams — eliminating the main-thread block on long tasks.
  interface PendingBatch {
    id: string
    thinkingChunk: string
    deltaChunk: string
    newToolSteps: ToolStep[]
    thinkingDone: boolean
    statusText: string | null
    markConnected: (() => void) | null
  }
  const pendingBatchRef = useRef<PendingBatch | null>(null)
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const flushPending = useCallback(() => {
    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current)
      flushTimerRef.current = null
    }
    const batch = pendingBatchRef.current
    if (!batch) return
    pendingBatchRef.current = null

    if (batch.markConnected) {
      batch.markConnected()
    }
    dispatch({
      type: 'BATCH_UPDATE',
      id: batch.id,
      thinkingChunk: batch.thinkingChunk,
      deltaChunk: batch.deltaChunk,
      newToolSteps: batch.newToolSteps,
      thinkingDone: batch.thinkingDone,
      statusText: batch.statusText,
    })
  }, [])

  const clearFlushTimer = useCallback(() => {
    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current)
      flushTimerRef.current = null
    }
    pendingBatchRef.current = null
  }, [])

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

          // Browser chip = REAL jailed-browser page state (backend probe) AND-ed with
          // "this conversation used the browser". A failed browser_navigate / web_search
          // leaves browser_live=false → no false chip; another conversation's open page
          // does NOT light THIS chat (browserUsedRef gates it). NEVER keyed off a tool
          // name alone.
          setLiveBrowserActive(browserUsedRef.current && !!runtimeStatus.browser_live)

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

          // Adopt THIS turn's mirror row ONLY once it flips to a terminal status.
          // Keyed on the in-flight task_id (not a fragile count of assistant rows):
          // the daemon upserts the growing answer as status='streaming' every 12
          // deltas, and the old count heuristic adopted that IN-FLIGHT partial as
          // "final" — sealing the live bubble mid-sentence and tearing down the
          // stream on any turn slow enough that the WS `done` frame hadn't arrived
          // within a 2s tick (slow LLM / long browser tool). A row with
          // status !== 'streaming' (complete, or the ⚠ engine-error / ⏹ cancel note)
          // is the real end of THIS turn.
          const finalRow = (detail.messages ?? []).find(
            m => m.role === 'assistant'
              && m.task_id === currentTaskIdRef.current
              && m.status !== 'streaming',
          )
          if (finalRow) {
            // ADOPT_FINAL has an isStreaming guard in the reducer — if the WS already
            // produced STREAM_DONE for this turn, the action is a no-op (no double-render).
            dispatch({
              type: 'ADOPT_FINAL',
              id: currentAssistantId,
              renderedHtml: renderMarkdown((finalRow.content ?? '').trim()),
            })
            setConversationsTick(t => t + 1) // turn persisted → refresh the recents list
            streamRef.current?.close()
            streamRef.current = null
            activeAssistantIdRef.current = null
            currentTaskIdRef.current = null
            sessionStorage.removeItem(SS_TASK_ID)
            setReconnecting(false)
            // liveBrowserActive stays ON: the jailed browser session (and the
            // "Ver en vivo" chip) persists for the whole conversation.
            clearPoll()
          }
        })
        .catch(() => { /* stale convId or transient error — keep polling */ })
    }, 2_000)
  }, [clearPoll])

  const stopStream = useCallback(() => {
    clearPoll()
    // Flush any coalesced frames before closing so no data is silently dropped.
    flushPending()
    clearFlushTimer()
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
    // NOT resetting liveBrowserActive here: stopStream also runs on manual stop
    // mid-conversation, and the live chip must persist per conversation. The
    // reset lives in startNew/loadConversation (conversation switches).
  }, [clearPoll, flushPending, clearFlushTimer])

  const startNew = useCallback(() => {
    stopStream()
    sessionStorage.removeItem(SS_CONV_ID)
    sessionStorage.removeItem(SS_TASK_ID)
    sessionStorage.removeItem(SS_AGENT_ID)
    setLiveBrowserActive(false)
    browserUsedRef.current = false  // new conversation — forget the browser marker
    dispatch({ type: 'RESET' })
  }, [stopStream])

  const startNewWithAgent = useCallback((agentId: string) => {
    stopStream()
    sessionStorage.removeItem(SS_CONV_ID)
    sessionStorage.removeItem(SS_TASK_ID)
    sessionStorage.setItem(SS_AGENT_ID, agentId)
    dispatch({ type: 'RESET' })
    dispatch({ type: 'SET_AGENT_ID', agentId })
  }, [stopStream])

  // Persist convId and agentId to sessionStorage whenever they change so a refresh can restore them.
  useEffect(() => {
    if (state.convId) {
      sessionStorage.setItem(SS_CONV_ID, state.convId)
    }
  }, [state.convId])

  useEffect(() => {
    if (state.agentId) {
      sessionStorage.setItem(SS_AGENT_ID, state.agentId)
    }
  }, [state.agentId])

  // On mount: restore the last conversation and, if a task was streaming, re-attach.
  // Also starts the polling safety-net so that long silent tool calls never freeze
  // the UI and the final answer is always rendered even if the WS done frame was missed.
  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true

    const savedConvId = sessionStorage.getItem(SS_CONV_ID)
    const savedTaskId = sessionStorage.getItem(SS_TASK_ID)
    const savedAgentId = sessionStorage.getItem(SS_AGENT_ID)

    if (savedAgentId) {
      dispatch({ type: 'SET_AGENT_ID', agentId: savedAgentId })
    }

    if (!savedConvId) return

    // Load the conversation history first.
    getConversation(savedConvId)
      .then(detail => {
        // The in-flight turn's PARTIAL (status='streaming') for the task we are about
        // to reattach: do NOT render it as a finished bubble — seed it into the live
        // streaming bubble below (mirror-first: shows instantly = no blank on refresh,
        // and the SSE replay is de-duped against it by length). A 'complete' row IS
        // rendered statically (the turn finished).
        const partialMsg = savedTaskId
          ? (detail.messages ?? []).find(
              m => m.role === 'assistant' && m.task_id === savedTaskId && m.status === 'streaming',
            )
          : undefined
        const streamingPartial: string | null = partialMsg ? (partialMsg.content ?? '') : null
        const messages: ChatMessage[] = (detail.messages ?? [])
          .filter(m => m.role === 'user' || m.role === 'assistant')
          .map((m): ChatMessage | null => {
            if (m.role === 'user') {
              return { type: 'user' as const, id: genUUID(), text: m.content ?? '' }
            }
            if (m.task_id && m.task_id === savedTaskId && m.status === 'streaming') {
              return null
            }
            return {
              type: 'assistant' as const,
              id: genUUID(),
              // Carry the backend task_id so we can detect "this turn is already
              // in the mirror" and skip re-subscribing to the stream.
              taskId: m.task_id ?? null,
              thinkingText: '',
              thinkingDone: true,
              toolSteps: [],
              activityText: '',
              renderedHtml: renderMarkdown(m.content ?? ''),
              isStreaming: false,
            }
          })
          .filter((m): m is ChatMessage => m !== null)
        dispatch({ type: 'LOAD_MESSAGES', convId: savedConvId, messages })

        // NOTE: the browser chip is NOT seeded from the global browser_live on mount —
        // that probe cannot attribute the live page to THIS conversation, so seeding it
        // would falsely light the chip on reload of a chat that never used the browser
        // while another one has a page open. If the reattached turn issues a browser_*
        // frame, browserUsedRef flips and the poll lights the chip honestly.

        // If a task stream was in-flight, decide whether to re-attach.
        if (savedTaskId) {
          // Check if the mirror already contains the final answer for savedTaskId.
          // If yes: the turn completed before we got here — render from the mirror
          // and do NOT re-subscribe (the replay would duplicate the message).
          const alreadyFinalized = messages.some(
            m => m.type === 'assistant' && m.taskId === savedTaskId,
          )

          if (alreadyFinalized) {
            // Turn is done — clean up the in-flight state and stay idle.
            sessionStorage.removeItem(SS_TASK_ID)
            return
          }

          // Turn still in progress — re-attach to the stream.
          // If a streaming bubble with this taskId already exists in the loaded
          // messages (shouldn't happen for a mid-turn refresh, but be safe),
          // reuse it; otherwise create a new one.
          const existingBubble = messages.find(
            m => m.type === 'assistant' && m.taskId === savedTaskId,
          ) as Extract<ChatMessage, { type: 'assistant' }> | undefined

          const assistantMsgId = existingBubble ? existingBubble.id : genUUID()
          if (!existingBubble) {
            dispatch({ type: 'ADD_ASSISTANT', id: assistantMsgId, taskId: savedTaskId })
          }
          // Mirror-first: seed the live bubble with the persisted partial so it shows
          // INSTANTLY (no blank on refresh) using the SAME streaming animation. The SSE
          // replay re-sends the full run from seq 0, so we skip the first
          // streamingPartial.length chars of replayed answer deltas to avoid doubling.
          let replaySkip = 0
          if (streamingPartial) {
            dispatch({ type: 'DELTA', id: assistantMsgId, chunk: streamingPartial })
            replaySkip = streamingPartial.length
          }
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

          // Accumulate incoming WS frames into pendingBatchRef and schedule a
          // FLUSH_INTERVAL_MS timeout. The flush emits a single BATCH_UPDATE
          // dispatch covering all frames collected since the last flush — this
          // limits React re-renders to ~8/sec no matter how fast the backend streams.
          const scheduleBatchFlush = () => {
            if (flushTimerRef.current === null) {
              flushTimerRef.current = setTimeout(flushPending, FLUSH_INTERVAL_MS)
            }
          }

          const callbacks: StreamCallbacks = {
            onDelta(chunk) {
              // De-dup the broker replay against the mirror partial we already seeded:
              // skip the first replaySkip chars of replayed answer deltas, keep the rest.
              if (replaySkip > 0) {
                if (chunk.length <= replaySkip) { replaySkip -= chunk.length; return }
                chunk = chunk.slice(replaySkip)
                replaySkip = 0
              }
              const b = pendingBatchRef.current
              if (b && b.id === assistantMsgId) {
                b.deltaChunk += chunk
                b.markConnected = markConnected
              } else {
                pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: '', deltaChunk: chunk, newToolSteps: [], thinkingDone: false, statusText: null, markConnected }
              }
              scheduleBatchFlush()
            },
            onThinking(chunk) {
              const b = pendingBatchRef.current
              if (b && b.id === assistantMsgId) {
                b.thinkingChunk += chunk
                b.markConnected = markConnected
              } else {
                pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: chunk, deltaChunk: '', newToolSteps: [], thinkingDone: false, statusText: null, markConnected }
              }
              scheduleBatchFlush()
            },
            onToolCall(frame: Extract<StreamFrame, { kind: 'tool_call' }>) {
              const d = frame.tool_call ?? (frame as Record<string, unknown>)
              const name = (d.tool as string | undefined) ?? (d.tool_name as string | undefined) ?? 'herramienta'
              const label = (d.label as string | undefined) ?? String(name).replace(/_/g, ' ')
              const target = String((d.target as string | undefined) ?? '').slice(0, 80)
              // Mark that THIS conversation used the browser (necessary condition). The
              // chip lights only when this AND browser_live (a real live page) are true
              // — a browser_* that fails to launch never lights it.
              if (String(name).startsWith('browser')) browserUsedRef.current = true
              const step: ToolStep = { name, label, target }
              // Flush any accumulated text before the tool step so the segment boundary
              // is clean (matches vanilla segmentStart behaviour — activityText resets on TOOL_CALL).
              flushPending()
              const b = pendingBatchRef.current
              if (b && b.id === assistantMsgId) {
                b.newToolSteps.push(step)
                b.thinkingDone = true
                b.markConnected = markConnected
              } else {
                pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: '', deltaChunk: '', newToolSteps: [step], thinkingDone: true, statusText: null, markConnected }
              }
              scheduleBatchFlush()
            },
            onStatus(msg) {
              const b = pendingBatchRef.current
              if (b && b.id === assistantMsgId) {
                b.statusText = msg
                b.markConnected = markConnected
              } else {
                pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: '', deltaChunk: '', newToolSteps: [], thinkingDone: false, statusText: msg, markConnected }
              }
              scheduleBatchFlush()
            },
            onDone() {
              // Flush any buffered frames BEFORE finalising so no data is lost.
              flushPending()
              clearFlushTimer()
              clearPoll()
              dispatch({ type: 'STREAM_DONE', id: assistantMsgId })
              setConversationsTick(t => t + 1) // turn persisted → refresh the recents list
              streamRef.current = null
              activeAssistantIdRef.current = null
              currentTaskIdRef.current = null
              sessionStorage.removeItem(SS_TASK_ID)
              setReconnecting(false)
              // Turn ended → the cycle reaps its confined-browser session
              // (cleanup_thread_browser_session), so the chip (the agent is actively
              // using the browser NOW) goes off, staying coherent with "En vivo".
              setLiveBrowserActive(false)
            },
            onError(_msg) {
              // On WS error after re-attach, keep the poll running — the task may
              // still be running and the mirror will eventually have the answer.
              // Show a neutral "working" status rather than an error bar so the user
              // is not alarmed while the poll continues watching for the final answer.
              // CRITICAL: do NOT delete SS_TASK_ID here. A transient WS error (idle
              // close, handshake gap, a 2nd refresh mid-reconnect) is NOT the task
              // ending — the task is still running server-side. Keeping the handle
              // lets the NEXT mount/refresh re-attach and the broker replay. The
              // handle is cleared only on a real terminal done (onDone) or when the
              // poll adopts the final answer (ADOPT_FINAL). Deleting it here was the
              // bug that made "2 refreshes kill the chat until it feels like coming
              // back": once gone, no refresh could ever reconnect.
              flushPending()
              clearFlushTimer()
              dispatch({ type: 'STATUS_STREAMING', text: 'Trabajando…' })
              streamRef.current = null
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

  // Clear the poll and any pending flush timer on unmount.
  useEffect(() => {
    return () => {
      clearPoll()
      clearFlushTimer()
    }
  }, [clearPoll, clearFlushTimer])

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim()) return

    stopStream()

    // Own the conversation id client-side (generate it locally before the first send)
    let convId = state.convId
    const isNewConversation = !convId
    if (!convId) {
      convId = genUUID()
      dispatch({ type: 'SET_CONV_ID', convId })
    }

    const userMsgId = genUUID()
    const assistantMsgId = genUUID()

    dispatch({ type: 'ADD_USER', id: userMsgId, text })
    dispatch({ type: 'STATUS_SENDING' })

    // Include agent_id only on the first message of a conversation so the backend
    // can bind it. On subsequent messages in the same conversation, the backend
    // already knows the agent; sending it again is harmless but we omit it for clarity.
    const agentIdToSend = isNewConversation ? (state.agentId ?? undefined) : undefined

    let taskId: string
    try {
      const res = await postChat({
        conversation_id: convId,
        user_message: text,
        dedup_key: `chat:${Date.now()}:${Math.random().toString(36).slice(2)}`,
        ...(agentIdToSend ? { agent_id: agentIdToSend } : {}),
      })
      taskId = res.task_id
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Error al enviar'
      dispatch({ type: 'STATUS_ERROR', message: msg })
      return
    }

    dispatch({ type: 'ADD_ASSISTANT', id: assistantMsgId, taskId })
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

    // Accumulate incoming WS frames into pendingBatchRef and schedule a
    // FLUSH_INTERVAL_MS timeout. The flush emits a single BATCH_UPDATE
    // dispatch covering all frames collected since the last flush — this
    // limits React re-renders to ~8/sec no matter how fast the backend streams.
    const scheduleBatchFlush = () => {
      if (flushTimerRef.current === null) {
        flushTimerRef.current = setTimeout(flushPending, FLUSH_INTERVAL_MS)
      }
    }

    const callbacks: StreamCallbacks = {
      onDelta(chunk) {
        const b = pendingBatchRef.current
        if (b && b.id === assistantMsgId) {
          b.deltaChunk += chunk
        } else {
          pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: '', deltaChunk: chunk, newToolSteps: [], thinkingDone: false, statusText: null, markConnected: null }
        }
        scheduleBatchFlush()
      },
      onThinking(chunk) {
        const b = pendingBatchRef.current
        if (b && b.id === assistantMsgId) {
          b.thinkingChunk += chunk
        } else {
          pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: chunk, deltaChunk: '', newToolSteps: [], thinkingDone: false, statusText: null, markConnected: null }
        }
        scheduleBatchFlush()
      },
      onToolCall(frame: Extract<StreamFrame, { kind: 'tool_call' }>) {
        // d can be the nested descriptor OR the frame itself — both shapes share tool/label/target.
        const d = frame.tool_call ?? (frame as Record<string, unknown>)
        const name = (d.tool as string | undefined) ?? (d.tool_name as string | undefined) ?? 'herramienta'
        const label = (d.label as string | undefined) ?? String(name).replace(/_/g, ' ')
        const target = String((d.target as string | undefined) ?? '').slice(0, 80)
        // Mark that THIS conversation used the browser (necessary condition; the chip
        // lights only when this AND browser_live are true).
        if (String(name).startsWith('browser')) browserUsedRef.current = true
        const step: ToolStep = { name, label, target }
        // Flush any accumulated text before the tool step so the segment boundary
        // is clean (matches vanilla segmentStart behaviour — activityText resets on TOOL_CALL).
        flushPending()
        const b = pendingBatchRef.current
        if (b && b.id === assistantMsgId) {
          b.newToolSteps.push(step)
          b.thinkingDone = true
        } else {
          pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: '', deltaChunk: '', newToolSteps: [step], thinkingDone: true, statusText: null, markConnected: null }
        }
        scheduleBatchFlush()
      },
      onStatus(msg) {
        const b = pendingBatchRef.current
        if (b && b.id === assistantMsgId) {
          b.statusText = msg
        } else {
          pendingBatchRef.current = { id: assistantMsgId, thinkingChunk: '', deltaChunk: '', newToolSteps: [], thinkingDone: false, statusText: msg, markConnected: null }
        }
        scheduleBatchFlush()
      },
      onDone() {
        // Flush any buffered frames BEFORE finalising so no data is lost.
        flushPending()
        clearFlushTimer()
        clearPoll()
        dispatch({ type: 'STREAM_DONE', id: assistantMsgId })
        setConversationsTick(t => t + 1) // turn persisted → refresh the recents list
        streamRef.current = null
        activeAssistantIdRef.current = null
        currentTaskIdRef.current = null
        sessionStorage.removeItem(SS_TASK_ID)
        // Turn ended → the cycle reaps its confined-browser session, so the chip (the
        // agent is actively using the browser NOW) goes off, coherent with "En vivo".
        setLiveBrowserActive(false)
      },
      onError(_msg) {
        // A terminal error frame (or transient teardown) arrived. Do NOT dispatch
        // STATUS_ERROR and do NOT delete SS_TASK_ID: STATUS_ERROR flips the bubble's
        // isStreaming=false, which makes the poll's ADOPT_FINAL a no-op (its guard
        // only seals a STILL-streaming bubble) → the persisted answer/refusal is
        // NEVER rendered = the "first attempt shows nothing, second works" bug.
        // Keep the bubble streaming and let the 2s mirror poll adopt the final
        // answer (success narrative, refusal, or the backend's "⚠ …" error turn).
        // Matches the re-attach onError (the proven-good path).
        flushPending()
        clearFlushTimer()
        dispatch({ type: 'STATUS_STREAMING', text: 'Trabajando…' })
        streamRef.current = null
      },
    }

    streamRef.current = openTaskStream(taskId, callbacks)
  }, [state.convId, stopStream, startPoll, clearPoll, flushPending, clearFlushTimer])

  const loadConversation = useCallback(async (id: string) => {
    stopStream()
    setLiveBrowserActive(false) // switching conversations — new live context
    browserUsedRef.current = false  // new conversation — forget the browser marker
    try {
      const detail = await getConversation(id)
      // Null-guard messages + content (parity with the mount-restore path): the
      // mirror can return a null content for some message kinds; m.content used
      // raw threw inside the try and made the conversation unopenable.
      const messages: ChatMessage[] = (detail.messages ?? [])
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .map(m => {
          if (m.role === 'user') {
            return { type: 'user' as const, id: genUUID(), text: m.content ?? '' }
          }
          return {
            type: 'assistant' as const,
            id: genUUID(),
            taskId: m.task_id ?? null,
            thinkingText: '',
            thinkingDone: true,
            toolSteps: [],
            activityText: '',
            renderedHtml: renderMarkdown(m.content ?? ''),
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
    agentId: state.agentId,
    messages: state.messages,
    status: state.status,
    reconnecting,
    liveBrowserActive,
    conversationsTick,
    sendMessage,
    startNew,
    startNewWithAgent,
    stopStream,
    loadConversation,
  }
}
