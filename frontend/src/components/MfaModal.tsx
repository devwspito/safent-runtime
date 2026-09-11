/**
 * Collects a scoped verification code; the parent owns the server decision.
 * Pending requests and retryable errors stay in the same dialog.
 */
import { Dialog } from '@base-ui/react/dialog'
import { useId, useRef, useState, useEffect } from 'react'
import { X } from 'lucide-react'
import { useT } from '../lib/i18n'

export type MfaTier = 'mfa'
export interface MfaFactors { totp: string }
export interface MfaModalProps {
  title: string
  onSign(factors: MfaFactors): void
  onCancel(): void
  loading?: boolean
  error?: string
}

export default function MfaModal({ title, onSign, onCancel, loading = false, error }: MfaModalProps) {
  const t = useT()
  const [totp, setTotp] = useState('')
  const [inlineError, setInlineError] = useState('')
  const totpRef = useRef<HTMLInputElement>(null)
  const submitted = useRef(false)
  const fieldId = useId()
  const errorId = useId()
  const message = inlineError || error

  useEffect(() => {
    if (!loading) submitted.current = false
    if (error && !loading) {
      totpRef.current?.focus()
      totpRef.current?.select()
    }
  }, [loading, error])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (loading || submitted.current) return
    const code = totp.trim()
    if (!/^\d{6,8}$/.test(code)) {
      setInlineError(t('mfa.err.empty'))
      totpRef.current?.focus()
      return
    }
    setInlineError('')
    submitted.current = true
    onSign({ totp: code })
    // Legacy callers do not supply pending state; retain their retry contract.
    queueMicrotask(() => { submitted.current = false })
  }

  return (
    <Dialog.Root open onOpenChange={open => { if (!open && !loading) onCancel() }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="mfa-modal-backdrop" />
        <Dialog.Popup className="mfa-modal" initialFocus={totpRef} aria-busy={loading}>
          <div className="mfa-modal__header">
            <Dialog.Title className="mfa-modal__title">{title}</Dialog.Title>
            <button type="button" className="mfa-modal__close" aria-label={t('mfa.btn.cancel')}
              disabled={loading} onClick={onCancel}><X size={16} aria-hidden /></button>
          </div>
          <form className="mfa-modal__body" onSubmit={handleSubmit}>
            <div className="mfa-modal__field">
              <label htmlFor={fieldId} className="cv-label">{t('mfa.title.code')}</label>
              <input id={fieldId} ref={totpRef} className="cv-input" inputMode="numeric"
                autoComplete="one-time-code" maxLength={8} placeholder={t('mfa.placeholder')}
                aria-describedby={message ? errorId : undefined} aria-invalid={!!message}
                disabled={loading} value={totp}
                onChange={e => { setTotp(e.target.value.replace(/[^0-9]/g, '')); setInlineError('') }} />
              {message && <p id={errorId} role="alert" className="mfa-modal__inline-error">{message}</p>}
            </div>
            <div className="mfa-modal__actions">
              <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm" disabled={loading}
                onClick={onCancel}>{t('mfa.btn.cancel')}</button>
              <button type="submit" className="cv-btn cv-btn--primary cv-btn--sm"
                disabled={loading}>{t('mfa.btn.confirm')}</button>
            </div>
          </form>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
