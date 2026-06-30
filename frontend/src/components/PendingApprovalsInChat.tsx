/**
 * PendingApprovalsInChat — polls for HITL approvals and renders those that
 * belong to the currently active conversation inside the chat message list.
 *
 * Flash prevention:
 *   - Cards are not rendered until the FIRST poll resolves (loaded flag).
 *   - Rows already resolved (no longer in the pending list) are filtered out.
 *   - Conversation matching only runs once convId is available.
 *   - Transient poll failures keep the last known list visible.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { listPendingApprovals, getPolicies } from '../api/client'
import type { PendingApproval } from '../api/types'
import ApprovalCard from './ApprovalCard'

const POLL_INTERVAL_MS = 3000

// Backend approval window is 1800 s (30 min). Use 31 min as the client-side
// cut-off so we never render a card the backend will immediately reject.
const APPROVAL_MAX_AGE_MS = 31 * 60 * 1000

function isApprovalFresh(createdAt: string | null | undefined): boolean {
  if (!createdAt) return true // unknown age — keep for back-compat
  const age = Date.now() - new Date(createdAt).getTime()
  return age < APPROVAL_MAX_AGE_MS
}

interface PendingApprovalsInChatProps {
  currentThreadId: string | null
  /** Incremented externally (e.g. on message send) to force an immediate refresh. */
  refreshTick: number
}

export default function PendingApprovalsInChat({
  currentThreadId,
  refreshTick,
}: PendingApprovalsInChatProps) {
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  const [mfaDisabled, setMfaDisabled] = useState(false)
  // Do not render any card until at least one poll has completed.
  const [loaded, setLoaded] = useState(false)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    // Skip if convId hasn't resolved yet — wait until we have a thread to match.
    // (Orphan approvals with no conversation_id are still shown regardless.)
    try {
      const [all, pol] = await Promise.all([listPendingApprovals(), getPolicies()])
      if (!Array.isArray(all)) return

      // Show approvals belonging to the active conversation, PLUS orphan ones
      // (conversation_id null/empty) that may come from scheduled/autonomous
      // cycles — they are never attached to a thread but still block the agent.
      // Age guard: discard anything older than APPROVAL_MAX_AGE_MS so stale
      // ghost cards never render even if the backend poll lags a cycle.
      const filtered = all.filter(a => {
        if (!isApprovalFresh(a.created_at)) return false
        if (!a.conversation_id) return true
        // Only match conversation-scoped approvals once we know the thread id.
        return currentThreadId !== null && a.conversation_id === currentThreadId
      })

      setApprovals(filtered)
      setMfaDisabled(pol.mfa_on_dangers === false)
      setLoaded(true)
    } catch {
      // Transient failure — keep last known approvals visible; don't mark loaded.
    }
  }, [currentThreadId])

  // Start/restart poll whenever the active thread changes.
  useEffect(() => {
    if (intervalRef.current !== null) clearInterval(intervalRef.current)
    // Reset loaded so we don't flash stale cards from a previous thread
    setLoaded(false)
    setApprovals([])
    void load()
    intervalRef.current = setInterval(() => { void load() }, POLL_INTERVAL_MS)
    return () => {
      if (intervalRef.current !== null) clearInterval(intervalRef.current)
    }
  }, [load])

  // Force immediate refresh when the parent bumps refreshTick (e.g. on send).
  useEffect(() => {
    if (refreshTick > 0) void load()
  }, [refreshTick, load])

  // Do not render anything until the first successful poll.
  if (!loaded || approvals.length === 0) return null

  return (
    <div
      className="cv-list"
      aria-label="Aprobaciones pendientes en esta conversación"
      aria-live="polite"
    >
      {approvals.map(a => (
        <ApprovalCard
          key={a.proposal_id}
          approval={a}
          mfaDisabled={mfaDisabled}
          onResolved={() => { void load() }}
        />
      ))}
    </div>
  )
}
