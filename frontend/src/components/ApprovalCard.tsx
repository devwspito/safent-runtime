import { useEffect, useId, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'
import { ChevronDown, KeyRound, ShieldCheck } from 'lucide-react'
import { ApiError, resolveApproval } from '../api/client'
import type { PendingApproval } from '../api/types'
import MfaModal from './MfaModal'
import { Button } from './ui/Button'
import { useT, useLocale, approvalTitle } from '../lib/i18n'
import css from './ApprovalCard.module.css'

export interface ApprovalCardProps {
  approval: PendingApproval
  onResolved(): void
}

type State = 'idle' | 'code' | 'enroll' | 'allowing' | 'denying' | 'resolved' | 'expired'

/** A scoped decision, not a grant of general permissions. The server gate
 * remains authoritative for MFA, enterprise routing and single consumption. */
export default function ApprovalCard({ approval, onResolved }: ApprovalCardProps) {
  const t = useT()
  const { locale } = useLocale()
  const navigate = useNavigate()
  const id = useId()
  const [state, setState] = useState<State>('idle')
  const [error, setError] = useState('')
  // Guard synchronously: a double click can precede React's next render.
  const submitted = useRef(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const previousState = useRef(state)
  const requiresCode = approval.required_level !== 'simple'
  const enterprise = approval.route === 'enterprise'
  const title = approvalTitle(approval.kind, approval.summary, locale)
  const parameters = Object.entries(approval.parameters ?? {})
  const busy = state === 'allowing' || state === 'denying'
  const terminal = state === 'resolved' || state === 'expired'

  useEffect(() => {
    if (state === 'idle' && previousState.current !== 'idle') trigger.current?.focus()
    previousState.current = state
  }, [state])

  async function decide(decision: 'once' | 'deny', totp?: string) {
    if (submitted.current || terminal || (enterprise && decision === 'once')) return
    submitted.current = true
    setError('')
    setState(decision === 'once' ? 'allowing' : 'denying')
    try {
      await resolveApproval(approval.proposal_id, decision,
        decision === 'once' ? { totp: totp ?? null } : undefined)
      setState('resolved')
      // Approval acknowledgement is not proof of tool execution. The chat's
      // tool-result event, not `live`, establishes the outcome of the action.
      sileo.success({ title: t(decision === 'once' ? 'approval.toast.allowed' : 'approval.toast.denied') })
      onResolved()
    } catch (err) {
      submitted.current = false
      const code = err instanceof ApiError ? err.code : undefined
      if (code === 'proposal_invalid' || code === 'expired') {
        setState('expired')
      } else {
        setState(decision === 'once' && requiresCode ? 'code' : 'idle')
        setError(t(code === 'invalid_totp' ? 'mfa.err.invalid'
          : decision === 'once' ? 'approval.err.allow' : 'approval.err.deny'))
      }
    }
  }

  function approve() {
    if (busy || terminal || submitted.current || enterprise) return
    setError('')
    if (requiresCode) setState(approval.mfa_enrolled === false ? 'enroll' : 'code')
    else void decide('once')
  }

  return (
    <>
      <section className={css.card} aria-labelledby={`${id}-title`} aria-describedby={`${id}-scope`} aria-busy={busy}>
        <header className={css.header}>
          <span className={css.icon}><ShieldCheck size={18} aria-hidden /></span>
          <div className={css.heading}>
            <span className={css.eyebrow}>{t('approval.request')}</span>
            <h3 id={`${id}-title`} className={css.title}>{title}</h3>
          </div>
          {requiresCode && <span className={css.verification}><KeyRound size={12} aria-hidden />{t('approval.verification')}</span>}
        </header>

        {approval.summary !== title && <p className={css.description}>{approval.summary}</p>}
        <div className={css.scope} id={`${id}-scope`}>
          <div><span>{t('approval.scope.label')}</span><strong>{t('approval.scope.once')}</strong></div>
          {approval.target && <div><span>{t('approval.target')}</span><code>{approval.target}</code></div>}
          {!approval.conversation_id && <div><span>{t('approval.origin')}</span><strong>{t('approval.origin.autonomous')}</strong></div>}
        </div>

        {(parameters.length > 0 || approval.technical_detail) && (
          <details className={css.details}>
            <summary><ChevronDown size={14} aria-hidden />{t('approval.details.toggle')}</summary>
            {approval.technical_detail && <p className={css.technical}>{approval.technical_detail}</p>}
            {parameters.length > 0 && <dl className={css.parameters}>
              {parameters.map(([key, value]) => <div key={key}>
                <dt>{key}</dt><dd>{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}</dd>
              </div>)}
            </dl>}
          </details>
        )}

        {enterprise && <p className={css.notice}>{t('approval.enterprise')}</p>}
        {state === 'enroll' && <div className={css.notice} role="status">
          <p>{t('approval.enroll.prompt')}</p>
          <Button size="sm" onClick={() => navigate('/sistema?tab=seguridad')}>{t('approval.enroll.cta')}</Button>
        </div>}
        {error && state !== 'code' && <p className={css.error} role="alert">{error}</p>}
        {terminal ? <footer className={css.footer} role="status">
          <span>{t(state === 'expired' ? 'approval.expired' : 'approval.resolved')}</span>
          <Button size="sm" variant="ghost" onClick={onResolved}>{t('approval.expired.close')}</Button>
        </footer> : <footer className={css.footer}>
          <span className={css.hint}>{t('approval.scope.hint')}</span>
          <div className={css.actions}>
            <Button size="sm" variant="ghost" disabled={busy} loading={state === 'denying'} onClick={() => void decide('deny')}>
              {t('approval.btn.deny')}
            </Button>
            {!enterprise && <Button ref={trigger} size="sm" variant="primary" disabled={busy || state === 'code'} loading={state === 'allowing'} onClick={approve}>
              {requiresCode && <KeyRound size={13} aria-hidden />}{t('approval.btn.allow')}
            </Button>}
          </div>
        </footer>}
      </section>
      {(state === 'code' || (state === 'allowing' && requiresCode)) && <MfaModal title={title}
        loading={state === 'allowing'} error={error}
        onSign={({ totp }) => { void decide('once', totp) }}
        onCancel={() => setState('idle')} />}
    </>
  )
}
