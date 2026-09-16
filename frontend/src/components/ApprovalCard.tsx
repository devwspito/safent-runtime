import { useEffect, useId, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { ChevronDown, ShieldCheck } from 'lucide-react'
import { ApiError, resolveApproval } from '../api/client'
import type { PendingApproval } from '../api/types'
import { Button } from './ui/Button'
import { useT, useLocale, approvalTitle } from '../lib/i18n'
import css from './ApprovalCard.module.css'

export interface ApprovalCardProps {
  approval: PendingApproval
  onResolved(): void
}

type State = 'idle' | 'allowing' | 'denying' | 'resolved' | 'expired'

/** A scoped decision, not a standing permission. Replacing a proposal mounts
 * fresh interaction state; the server still owns routing and consumption. */
export default function ApprovalCard(props: ApprovalCardProps) {
  return <ApprovalDecision key={props.approval.proposal_id} {...props} />
}

function ApprovalDecision({ approval, onResolved }: ApprovalCardProps) {
  const t = useT()
  const { locale } = useLocale()
  const id = useId()
  const [state, setState] = useState<State>('idle')
  const [error, setError] = useState('')
  // Guard synchronously: a double click can precede React's next render.
  const submitted = useRef(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const denyTrigger = useRef<HTMLButtonElement>(null)
  const lastDecision = useRef<'once' | 'deny'>('deny')
  const mounted = useRef(true)
  const previousState = useRef(state)
  const enterprise = approval.route === 'enterprise'
  const title = approvalTitle(approval.kind, approval.summary, locale)
  const parameters = Object.entries(approval.parameters ?? {})
  const busy = state === 'allowing' || state === 'denying'
  const terminal = state === 'resolved' || state === 'expired'

  useEffect(() => {
    const action = lastDecision.current === 'deny' ? denyTrigger.current : trigger.current
    if (state === 'idle' && previousState.current !== 'idle'
      && (document.activeElement === document.body || document.activeElement === action)) action?.focus()
    previousState.current = state
  }, [state])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  async function decide(decision: 'once' | 'deny') {
    if (submitted.current || terminal || (enterprise && decision === 'once')) return
    submitted.current = true
    lastDecision.current = decision
    setError('')
    setState(decision === 'once' ? 'allowing' : 'denying')
    try {
      await resolveApproval(approval.proposal_id, decision)
      if (!mounted.current) return
      setState('resolved')
      // Approval acknowledgement is not proof of tool execution. The chat's
      // tool-result event, not `live`, establishes the outcome of the action.
      sileo.success({ title: t(decision === 'once' ? 'approval.toast.allowed' : 'approval.toast.denied') })
      onResolved()
    } catch (err) {
      if (!mounted.current) return
      submitted.current = false
      const code = err instanceof ApiError ? err.code : undefined
      if (code === 'proposal_invalid' || code === 'expired') {
        setState('expired')
      } else {
        setState('idle')
        setError(t(decision === 'once' ? 'approval.err.allow' : 'approval.err.deny'))
      }
    }
  }

  function approve() {
    if (busy || terminal || submitted.current || enterprise) return
    setError('')
    void decide('once')
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
        {error && <p className={css.error} role="alert">{error}</p>}
        {terminal ? <footer className={css.footer} role="status">
          <span>{t(state === 'expired' ? 'approval.expired' : 'approval.resolved')}</span>
          <Button size="sm" variant="ghost" onClick={onResolved}>{t('approval.expired.close')}</Button>
        </footer> : <footer className={css.footer}>
          <span className={css.hint}>{t('approval.scope.hint')}</span>
          <div className={css.actions}>
            <Button ref={denyTrigger} size="sm" variant="ghost" disabled={busy} loading={state === 'denying'} onClick={() => void decide('deny')}>
              {t('approval.btn.deny')}
            </Button>
            {!enterprise && <Button ref={trigger} size="sm" variant="primary" disabled={busy} loading={state === 'allowing'} onClick={approve}>
              {t('approval.btn.allow')}
            </Button>}
          </div>
        </footer>}
      </section>
    </>
  )
}
