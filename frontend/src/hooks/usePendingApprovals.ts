import { useCallback, useEffect, useState } from 'react'
import { listPendingApprovals } from '../api/client'
import type { PendingApproval } from '../api/types'

/**
 * Pending status belongs to the approval gate, not the browser clock.
 * Different client-side age cutoffs used to hide valid approvals in one view
 * while another still displayed them. All consumers now use the server list.
 */
export function usePendingApprovals(pollMs = 6000, refreshKey: unknown = 0) {
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  const [isLoading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useEffect(() => {
    let alive = true
    let inFlight = false
    const poll = () => {
      if (inFlight) return
      inFlight = true
      listPendingApprovals()
        .then((a) => {
          if (!alive) return
          setApprovals(Array.isArray(a) ? a : [])
          setError(false)
          setLoading(false)
        })
        .catch(() => {
          if (!alive) return
          setError(true)
          setLoading(false)
          // Keep the last known list. An unavailable gate is not an empty one.
        })
        .finally(() => { inFlight = false })
    }
    poll()
    const id = setInterval(poll, pollMs)
    return () => { alive = false; clearInterval(id) }
  }, [pollMs, refreshKey, revision])
  return { approvals, isLoading, error, refresh }
}
