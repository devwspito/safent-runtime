import { usePendingApprovals } from '../hooks/usePendingApprovals'
import { useT } from '../lib/i18n'
import ApprovalCard from './ApprovalCard'

interface PendingApprovalsInChatProps {
  currentThreadId: string | null
  refreshTick: number
}

export default function PendingApprovalsInChat({ currentThreadId, refreshTick }: PendingApprovalsInChatProps) {
  const t = useT()
  const { approvals: all, error, refresh } = usePendingApprovals(3000, refreshTick)
  // Filter at render time, not in an async request closure. A late response
  // from a previous conversation can never put its approvals into this one.
  const approvals = all.filter(a => !a.conversation_id || a.conversation_id === currentThreadId)
  if (approvals.length === 0 && !error) return null

  return (
    <div className="cv-list" aria-label="Aprobaciones pendientes" aria-live="polite">
      {error && <p role="status">{t('approval.list_unavailable')}</p>}
      {approvals.map(approval => (
        <ApprovalCard key={approval.proposal_id} approval={approval}
          onResolved={refresh} />
      ))}
    </div>
  )
}
