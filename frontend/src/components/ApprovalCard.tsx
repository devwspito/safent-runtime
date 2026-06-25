/**
 * ApprovalCard — HITL approval widget.
 *
 * Rendered both inside SeguridadView (full list) and PendingApprovalsInChat
 * (filtered to the active conversation).
 *
 * State machine: idle | awaiting_code | needs_enrollment | resolving | error | expired
 *
 * Tier model (server-side classification via `approval.required_level`):
 *   simple — approve directly; no TOTP.
 *   mfa    — MfaModal collects TOTP before approving; if owner has NOT enrolled
 *             MFA yet, show inline enrollment nudge instead of opening the modal.
 */

import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'
import { Info, KeyRound, ShieldAlert } from 'lucide-react'
import { resolveApproval } from '../api/client'
import type { PendingApproval } from '../api/types'
import MfaModal from './MfaModal'
import { useT, useLocale, approvalTitle } from '../lib/i18n'

export interface ApprovalCardProps {
  approval: PendingApproval
  /** Legacy compat — no longer the gate; kept for PendingApprovalsInChat. */
  mfaDisabled?: boolean
  onResolved(): void
}

// ── Tier helpers ──────────────────────────────────────────────────────────────

type Tier = 'simple' | 'mfa' | 'destructive'

function deriveTier(approval: PendingApproval): Tier {
  if (approval.required_level === 'mfa') return 'mfa'
  return 'simple'
}

// ── Card state machine ────────────────────────────────────────────────────────

type CardState =
  | { phase: 'idle' }
  | { phase: 'awaiting_code' }
  | { phase: 'needs_enrollment' }
  | { phase: 'resolving'; action: 'allow' | 'deny' }
  | { phase: 'error'; action: 'allow' | 'deny'; message: string }
  | { phase: 'expired' }

// ── Sub-components ────────────────────────────────────────────────────────────

function RiskBadge({ tier }: { tier: Tier }) {
  const t = useT()

  if (tier === 'mfa') {
    return (
      <span
        className="seg-pol-badge seg-approval-card__badge seg-approval-card__badge--manual"
        aria-label={t('approval.badge.manual')}
      >
        <ShieldAlert size={14} aria-hidden="true" />
        {t('approval.badge.manual')}
      </span>
    )
  }

  if (tier === 'destructive') {
    return (
      <span
        className="seg-pol-badge seg-approval-card__badge seg-approval-card__badge--destructive"
        aria-label={t('approval.badge.destructive')}
      >
        <ShieldAlert size={14} aria-hidden="true" />
        {t('approval.badge.destructive')}
      </span>
    )
  }

  return (
    <span
      className="seg-pol-badge seg-approval-card__badge seg-approval-card__badge--attention"
      aria-label={t('approval.badge.attention')}
    >
      <Info size={14} aria-hidden="true" />
      {t('approval.badge.attention')}
    </span>
  )
}

function EnrollmentNudge({ onDismiss }: { onDismiss: () => void }) {
  const t = useT()
  const navigate = useNavigate()

  return (
    <div className="seg-approval-card__enroll-nudge" role="status">
      <p className="seg-approval-card__why" style={{ marginBottom: 'var(--sp-3)' }}>
        {t('approval.enroll.prompt')}
      </p>
      <div className="seg-approval-card__actions">
        <button
          type="button"
          className="cv-btn cv-btn--ghost cv-btn--sm"
          onClick={onDismiss}
        >
          {t('approval.enroll.later')}
        </button>
        <button
          type="button"
          className="cv-btn cv-btn--primary cv-btn--sm"
          onClick={() => navigate('/seguridad')}
        >
          {t('approval.enroll.cta')}
        </button>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function ApprovalCard({
  approval,
  onResolved,
}: ApprovalCardProps) {
  const t = useT()
  const { locale } = useLocale()
  const [cardState, setCardState] = useState<CardState>({ phase: 'idle' })
  const detailsRef = useRef<HTMLDetailsElement>(null)

  const tier = deriveTier(approval)
  const isMfaTier = tier === 'mfa'

  // Derive the human-readable title from `kind`; fall back to `summary`.
  const humanTitle = approvalTitle(approval.kind, approval.summary, locale)
  // The `summary` from backend is the "why" body (one sentence of context).
  const whyBody = approval.summary !== humanTitle ? approval.summary : undefined

  const params = approval.parameters
  const paramEntries =
    params && typeof params === 'object' && !Array.isArray(params)
      ? Object.entries(params).slice(0, 8)
      : []

  // ── Tier modifier CSS class ─────────────────────────────────────────────
  const tierMod =
    tier === 'mfa' ? 'seg-approval-card--manual'
    : tier === 'destructive' ? 'seg-approval-card--destructive'
    : 'seg-approval-card--attention'

  // ── Handlers ───────────────────────────────────────────────────────────

  function handleApproveClick() {
    if (cardState.phase === 'resolving' || cardState.phase === 'expired') return

    if (isMfaTier) {
      const enrolled = approval.mfa_enrolled ?? true
      if (!enrolled) {
        setCardState({ phase: 'needs_enrollment' })
      } else {
        setCardState({ phase: 'awaiting_code' })
      }
    } else {
      void doApprove()
    }
  }

  async function doApprove(totp?: string) {
    setCardState({ phase: 'resolving', action: 'allow' })
    try {
      const res = await resolveApproval(approval.proposal_id, 'once', { totp: totp ?? null }) as {
        ok?: boolean
        live?: boolean
        decision?: string
      } | null | undefined
      // live=true  → LIVE block-and-resume: the blocked thread was signalled and the
      //              exact tool call is executing now → honest "ran" feedback.
      // live=false → POST: the turn had already ended or timed out before this approval
      //              arrived → the action did NOT run; tell the owner to ask again.
      // live=undefined (old server / non-D-Bus path) → treat as live (keep existing UX).
      const isLive = res == null || res.live !== false
      if (isLive) {
        // i18n key to add: 'approval.toast.executed' → 'Acción aprobada y ejecutada.'
        // Using inline until i18n.ts is updated (reserved effort).
        sileo.success({ title: 'Acción aprobada y ejecutada.' })
      } else {
        sileo.warning({
          title: 'La solicitud ya había caducado — acción no ejecutada.',
          description: 'El agente ya había terminado. Vuelve a pedírselo.',
        })
        setCardState({ phase: 'expired' })
        onResolved()
        return
      }
      onResolved()
    } catch (err) {
      const msg = err instanceof Error ? err.message : ''
      if (msg.includes('expired') || msg.includes('proposal_invalid')) {
        setCardState({ phase: 'expired' })
      } else {
        setCardState({ phase: 'error', action: 'allow', message: t('approval.err.allow') })
        sileo.error({ title: t('approval.toast.err_allow') })
      }
    }
  }

  async function handleDeny() {
    if (cardState.phase === 'resolving' || cardState.phase === 'expired') return
    setCardState({ phase: 'resolving', action: 'deny' })
    try {
      await resolveApproval(approval.proposal_id, 'deny')
      sileo.success({ title: t('approval.toast.denied') })
      onResolved()
    } catch (err) {
      const msg = err instanceof Error ? err.message : ''
      if (msg.includes('expired') || msg.includes('proposal_invalid')) {
        setCardState({ phase: 'expired' })
      } else {
        setCardState({ phase: 'error', action: 'deny', message: t('approval.err.deny') })
        sileo.error({ title: t('approval.toast.err_deny') })
      }
    }
  }

  function handleMfaSign({ totp }: { totp: string }) {
    setCardState({ phase: 'idle' })
    void doApprove(totp)
  }

  function handleMfaCancel() {
    setCardState({ phase: 'idle' })
  }

  const isResolving = cardState.phase === 'resolving'
  const isExpired   = cardState.phase === 'expired'
  const isError     = cardState.phase === 'error'
  const actionsDisabled = isResolving || isExpired

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <>
      <div
        className={`seg-approval-card ${tierMod}`}
        role="alertdialog"
        aria-label={humanTitle}
        aria-busy={isResolving}
      >
        {/* Head: title + risk badge */}
        <div className="seg-approval-card__head">
          <h3 className="seg-approval-card__title">{humanTitle}</h3>
          <RiskBadge tier={tier} />
        </div>

        {/* Body: one-sentence "why" from backend */}
        {whyBody && (
          <p className="seg-approval-card__why">{whyBody}</p>
        )}

        {/* Collapsible technical details */}
        {paramEntries.length > 0 && (
          <details
            ref={detailsRef}
            className="seg-details seg-approval-card__details"
          >
            <summary>
              <svg
                className="seg-approval-card__chevron"
                width="13"
                height="13"
                viewBox="0 0 13 13"
                fill="none"
                aria-hidden="true"
              >
                <path d="M4 5l2.5 2.5L9 5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              {t('approval.details.toggle')}
            </summary>
            <dl className="seg-approval-card__params">
              {paramEntries.map(([k, v]) => (
                <div key={k} className="seg-approval-card__param-row">
                  <dt>{k}</dt>
                  <dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}

        {/* Enrollment nudge (mfa not enrolled) */}
        {cardState.phase === 'needs_enrollment' && (
          <EnrollmentNudge onDismiss={() => setCardState({ phase: 'idle' })} />
        )}

        {/* Inline error band */}
        {isError && (
          <div className="seg-approval-card__error-band" role="alert">
            <span>{cardState.message}</span>
            <div style={{ display: 'flex', gap: 'var(--sp-2)' }}>
              <button
                type="button"
                className="cv-btn cv-btn--ghost cv-btn--sm"
                onClick={() => setCardState({ phase: 'idle' })}
              >
                {t('approval.err.cancel')}
              </button>
              <button
                type="button"
                className="cv-btn cv-btn--secondary cv-btn--sm"
                onClick={() => {
                  if (cardState.action === 'allow') void doApprove()
                  else void handleDeny()
                }}
              >
                {t('approval.err.retry')}
              </button>
            </div>
          </div>
        )}

        {/* Expired state */}
        {isExpired && (
          <div className="seg-approval-card__expired-band" role="status">
            <span>{t('approval.expired')}</span>
            <button
              type="button"
              className="cv-btn cv-btn--ghost cv-btn--sm"
              onClick={onResolved}
            >
              {t('approval.expired.close')}
            </button>
          </div>
        )}

        {/* Actions — hidden when showing enrollment nudge or expired */}
        {cardState.phase !== 'needs_enrollment' && !isExpired && !isError && (
          <div
            className="seg-approval-card__actions"
            role="group"
            aria-label="Acciones de aprobación"
          >
            <button
              className="cv-btn cv-btn--ghost cv-btn--sm"
              onClick={() => void handleDeny()}
              disabled={actionsDisabled}
              type="button"
            >
              {isResolving && cardState.action === 'deny'
                ? t('approval.btn.denying')
                : t('approval.btn.deny')}
            </button>

            <button
              className={`cv-btn cv-btn--sm ${tier === 'destructive' ? 'cv-btn--secondary' : 'cv-btn--primary'}`}
              onClick={handleApproveClick}
              disabled={actionsDisabled}
              type="button"
            >
              {isResolving && cardState.action === 'allow' ? (
                t('approval.btn.allowing')
              ) : isMfaTier ? (
                <>
                  <KeyRound size={14} aria-hidden="true" style={{ marginRight: 'var(--sp-1)' }} />
                  {t('approval.btn.allow_mfa')}
                </>
              ) : (
                t('approval.btn.allow')
              )}
            </button>
          </div>
        )}
      </div>

      {/* MfaModal — stays open until code confirmed or cancelled */}
      {cardState.phase === 'awaiting_code' && (
        <MfaModal
          title={humanTitle}
          onSign={handleMfaSign}
          onCancel={handleMfaCancel}
        />
      )}
    </>
  )
}
