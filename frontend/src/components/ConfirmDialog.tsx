/**
 * ConfirmDialog — accessible modal confirmation dialog.
 *
 * Features: focus-trap, Escape to cancel, aria-modal, aria-describedby.
 * Replaces all window.confirm() calls so they stay in the design system.
 *
 * Usage:
 *   const [confirm, ConfirmDialogNode] = useConfirmDialog()
 *   await confirm({ title: '…', description: '…', confirmLabel: '…' })
 *
 *   // or imperative with callback:
 *   confirm({ … }).then(ok => { if (ok) doThing() })
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useT } from '../lib/i18n'

export interface ConfirmOptions {
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  /** 'danger' renders the confirm button in red — use for destructive actions */
  variant?: 'default' | 'danger'
}

/**
 * Hook that returns [confirm, DialogNode].
 * Place <DialogNode /> anywhere in the component tree (portals to document.body).
 */
export function useConfirmDialog(): [
  (opts: ConfirmOptions) => Promise<boolean>,
  React.ReactNode,
] {
  const [state, setState] = useState<ConfirmOptions | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const pending = useRef<((approved: boolean) => void) | null>(null)
  useEffect(() => () => { pending.current?.(false); pending.current = null }, [])

  const confirm = useCallback((opts: ConfirmOptions): Promise<boolean> => {
    // Capture which element triggered the dialog so we can restore focus on close
    if (!pending.current) triggerRef.current = document.activeElement as HTMLButtonElement | null
    pending.current?.(false)
    return new Promise<boolean>(resolve => {
      pending.current = resolve
      setState(opts)
    })
  }, [])

  function close(ok: boolean) {
    pending.current?.(ok)
    pending.current = null
    setState(null)
    // Restore focus to the element that opened the dialog
    triggerRef.current?.focus()
  }

  const node = state ? (
    <ConfirmDialogUI
      title={state.title}
      description={state.description}
      confirmLabel={state.confirmLabel}
      cancelLabel={state.cancelLabel}
      variant={state.variant}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  ) : null

  return [confirm, node]
}

// ── Internal presentational component ────────────────────────────────────────

interface ConfirmDialogUIProps extends ConfirmOptions {
  onConfirm(): void
  onCancel(): void
}

function ConfirmDialogUI({
  title,
  description,
  confirmLabel,
  cancelLabel,
  variant = 'default',
  onConfirm,
  onCancel,
}: ConfirmDialogUIProps) {
  const t = useT()
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelBtnRef = useRef<HTMLButtonElement>(null)
  const confirmBtnRef = useRef<HTMLButtonElement>(null)
  const descId = useId()
  const titleId = useId()

  // Focus the cancel button on open (safer default for destructive confirmations)
  useEffect(() => {
    cancelBtnRef.current?.focus()
  }, [])

  // Escape key cancels
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        onCancel()
      }
      // Focus trap: keep Tab cycling within the dialog
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
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

  return createPortal(
    <div
      className="confirm-overlay"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget) onCancel() }}
      aria-hidden="false"
    >
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        className="confirm-card"
      >
        <h2 id={titleId} className="confirm-card__title">{title}</h2>
        {description && (
          <p id={descId} className="confirm-card__desc">{description}</p>
        )}
        <div className="confirm-card__actions">
          <button
            ref={cancelBtnRef}
            type="button"
            className="cv-btn cv-btn--ghost cv-btn--sm"
            onClick={onCancel}
          >
            {cancelLabel ?? t('dialog.cancel')}
          </button>
          <button
            ref={confirmBtnRef}
            type="button"
            className={`cv-btn cv-btn--sm ${variant === 'danger' ? 'cv-btn--danger cv-btn--danger-solid' : 'cv-btn--primary'}`}
            onClick={onConfirm}
          >
            {confirmLabel ?? t('dialog.confirm')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
