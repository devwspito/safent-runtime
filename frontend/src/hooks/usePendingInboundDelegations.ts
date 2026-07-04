import { useEffect, useState } from 'react'
import { listInboundDelegations } from '../api/client'
import type { InboundDelegation } from '../api/types'

/**
 * Polls pending inbound cross-human delegations (FASE 3 A2A) — cards asking
 * this owner to approve/reject work a colleague's assistant wants to hand to
 * this agent. Same posture as usePendingApprovals: the sidebar badge, the
 * Sistema tab badge and the Seguridad list all read from this one poll so
 * they never disagree.
 */
export function usePendingInboundDelegations(
  pollMs = 6000,
  refreshKey: unknown = 0,
): InboundDelegation[] {
  const [delegations, setDelegations] = useState<InboundDelegation[]>([])
  useEffect(() => {
    let alive = true
    const poll = () => {
      listInboundDelegations()
        .then((d) => {
          if (!alive) return
          setDelegations(Array.isArray(d) ? d : [])
        })
        .catch(() => { /* transient — keep last known list */ })
    }
    poll()
    const id = setInterval(poll, pollMs)
    return () => { alive = false; clearInterval(id) }
  }, [pollMs, refreshKey])
  return delegations
}
