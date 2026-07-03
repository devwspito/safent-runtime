import { useEffect, useState } from 'react'
import { listPendingApprovals } from '../api/client'
import type { PendingApproval } from '../api/types'

// The backend approval window is 600 s (10 min). Discard anything older
// client-side so ghost cards never render even if a poll cycle lags.
// null/absent created_at = keep (back-compat).
export const APPROVAL_MAX_AGE_MS = 11 * 60 * 1000

export function isApprovalFresh(createdAt: string | null | undefined): boolean {
  if (!createdAt) return true
  return Date.now() - new Date(createdAt).getTime() < APPROVAL_MAX_AGE_MS
}

/**
 * Polls the FRESH pending approvals. One freshness rule for every consumer —
 * the sidebar badge, the Sistema hub tab badge and the Seguridad list must
 * never disagree (a stale approval used to show "1" on the sidebar while
 * Seguridad said "nothing pending").
 */
export function usePendingApprovals(pollMs = 6000, refreshKey: unknown = 0): PendingApproval[] {
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  useEffect(() => {
    let alive = true
    const poll = () => {
      listPendingApprovals()
        .then((a) => {
          if (!alive) return
          setApprovals((Array.isArray(a) ? a : []).filter((x) => isApprovalFresh(x.created_at)))
        })
        .catch(() => { /* transient — keep last known list */ })
    }
    poll()
    const id = setInterval(poll, pollMs)
    return () => { alive = false; clearInterval(id) }
  }, [pollMs, refreshKey])
  return approvals
}
