/**
 * InboundDelegationCard — FASE 3 (A2A cross-human) HITL widget.
 *
 * Distinct from ApprovalCard: this is NOT the owner's own agent asking to run
 * an action — it is a COLLEAGUE'S assistant asking this owner's agent to pick
 * up work. Visually separated (Users icon, green "cross-human" accent instead
 * of the blue/amber/red tiers ApprovalCard uses) so the two never blur together.
 *
 * No MFA tier here (unlike ApprovalCard): resolve_inbound_delegation only
 * derives approved_by/rejected_by from the authenticated D-Bus channel —
 * the decision itself is a plain approve/reject, no TOTP step.
 */

import { useState } from 'react'
import { sileo } from 'sileo'
import { Users } from 'lucide-react'
import { resolveInboundDelegation } from '../api/client'
import type { InboundDelegation } from '../api/types'
import { useT } from '../lib/i18n'

export interface InboundDelegationCardProps {
  delegation: InboundDelegation
  onResolved(): void
}

type CardState =
  | { phase: 'idle' }
  | { phase: 'resolving'; action: 'approve' | 'reject' }
  | { phase: 'error'; action: 'approve' | 'reject'; message: string }

export default function InboundDelegationCard({
  delegation,
  onResolved,
}: InboundDelegationCardProps) {
  const t = useT()
  const [cardState, setCardState] = useState<CardState>({ phase: 'idle' })

  const isResolving = cardState.phase === 'resolving'
  const isError = cardState.phase === 'error'
  const actionsDisabled = isResolving

  async function resolve(decision: 'approve' | 'reject') {
    if (isResolving) return
    setCardState({ phase: 'resolving', action: decision })
    try {
      await resolveInboundDelegation(delegation.message_id, decision)
      sileo.success({
        title: decision === 'approve'
          ? t('delegation.toast.approved')
          : t('delegation.toast.rejected'),
      })
      onResolved()
    } catch {
      const message = decision === 'approve'
        ? t('delegation.err.approve')
        : t('delegation.err.reject')
      setCardState({ phase: 'error', action: decision, message })
      sileo.error({ title: message })
    }
  }

  const title = t('delegation.title').replace('{employee}', delegation.from_employee_id)

  return (
    <div
      className="seg-delegation-card"
      role="alertdialog"
      aria-label={title}
      aria-busy={isResolving}
    >
      <div className="seg-delegation-card__head">
        <span className="seg-delegation-card__badge" aria-hidden="true">
          <Users size={14} aria-hidden="true" />
          {t('delegation.badge')}
        </span>
      </div>

      <p className="seg-delegation-card__from">{title}</p>

      <blockquote className="seg-delegation-card__body">
        {delegation.body}
      </blockquote>

      {isError && (
        <div className="seg-approval-card__error-band" role="alert">
          <span>{cardState.message}</span>
          <button
            type="button"
            className="cv-btn cv-btn--secondary cv-btn--sm"
            onClick={() => void resolve(cardState.action)}
          >
            {t('approval.err.retry')}
          </button>
        </div>
      )}

      <div
        className="seg-delegation-card__actions"
        role="group"
        aria-label={t('delegation.actions_aria')}
      >
        <button
          type="button"
          className="cv-btn cv-btn--ghost cv-btn--sm"
          onClick={() => void resolve('reject')}
          disabled={actionsDisabled}
        >
          {isResolving && cardState.action === 'reject'
            ? t('delegation.btn.rejecting')
            : t('delegation.btn.reject')}
        </button>
        <button
          type="button"
          className="cv-btn cv-btn--primary cv-btn--sm"
          onClick={() => void resolve('approve')}
          disabled={actionsDisabled}
        >
          {isResolving && cardState.action === 'approve'
            ? t('delegation.btn.approving')
            : t('delegation.btn.approve')}
        </button>
      </div>
    </div>
  )
}
