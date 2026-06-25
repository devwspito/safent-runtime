/**
 * MfaModal — collects a TOTP verification code before a sensitive action.
 *
 * Interaction contract:
 *   - Stays OPEN if the code is incorrect; shows inline error + refocuses input.
 *   - Calls onSign when the code is ready to be submitted; the parent drives
 *     the API call and decides when to close.
 *   - Closes (onCancel) on Escape / backdrop click / Cancel button.
 */

import { createPortal } from 'react-dom'
import { useId, useRef, useState, useEffect } from 'react'
import { X } from 'lucide-react'
import { useT } from '../lib/i18n'

export type MfaTier = 'mfa'

export interface MfaFactors {
  totp: string
}

export interface MfaModalProps {
  title: string
  onSign(factors: MfaFactors): void
  onCancel(): void
}

export default function MfaModal({ title, onSign, onCancel }: MfaModalProps) {
  const t = useT()
  const [totp, setTotp] = useState('')
  const [inlineError, setInlineError] = useState('')
  const totpRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  const titleId = useId()
  const errorId = useId()

  useEffect(() => {
    totpRef.current?.focus()
  }, [])

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCancel()
        return
      }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }
    document.addEventListener('keydown', handleKey, true)
    return () => document.removeEventListener('keydown', handleKey, true)
  }, [onCancel])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const code = totp.trim()
    if (!code) {
      setInlineError(t('mfa.err.empty'))
      totpRef.current?.focus()
      return
    }
    setInlineError('')
    onSign({ totp: code })
  }

  return createPortal(
    <div
      className="mfa-modal-backdrop"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget) onCancel() }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="mfa-modal"
      >
        <div className="mfa-modal__header">
          <h2 id={titleId} className="mfa-modal__title">{title}</h2>
          <button
            type="button"
            className="mfa-modal__close"
            aria-label="Cerrar"
            onClick={onCancel}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <form className="mfa-modal__body" onSubmit={handleSubmit}>
          <div className="mfa-modal__field">
            <label htmlFor={`${titleId}-totp`} className="cv-label">
              {t('mfa.title.code')}
            </label>
            <input
              id={`${titleId}-totp`}
              ref={totpRef}
              className="cv-input"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={8}
              placeholder={t('mfa.placeholder')}
              aria-label={t('mfa.title.code')}
              aria-describedby={inlineError ? errorId : undefined}
              aria-invalid={!!inlineError}
              value={totp}
              onChange={e => {
                setTotp(e.target.value)
                if (inlineError) setInlineError('')
              }}
            />
            {inlineError && (
              <p id={errorId} role="alert" className="mfa-modal__inline-error">
                {inlineError}
              </p>
            )}
          </div>

          <div className="mfa-modal__actions">
            <button
              type="button"
              className="cv-btn cv-btn--ghost cv-btn--sm"
              onClick={onCancel}
            >
              {t('mfa.btn.cancel')}
            </button>
            <button
              type="submit"
              className="cv-btn cv-btn--primary cv-btn--sm"
            >
              {t('mfa.btn.confirm')}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
