/**
 * InstallScanModal — shown when a security scan returns requires_owner_approval.
 *
 * Displays the engine label (so the scan doesn't feel instant/fake), verdict,
 * score, and risk list. The owner can approve (→ triggers MFA collection via
 * MfaModal) or cancel. On approval the parent POSTs /security/decisions and
 * proceeds to install.
 */

import { createPortal } from 'react-dom'
import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import type { InstallScanResponse, InstallRisk } from '../api/types'
import Badge, { type BadgeVariant } from './Badge'

function verdictVariant(v: string): BadgeVariant {
  if (v === 'PASS') return 'ok'
  if (v === 'WARN') return 'warn'
  return 'danger'
}

function severityVariant(s: string): BadgeVariant {
  const lower = s.toLowerCase()
  if (lower === 'critical' || lower === 'high') return 'danger'
  if (lower === 'medium') return 'warn'
  return 'neutral'
}

interface InstallScanModalProps {
  scan: InstallScanResponse
  name: string
  onApprove(): void | Promise<void>
  onCancel(): void
}

export default function InstallScanModal({
  scan,
  name,
  onApprove,
  onCancel,
}: InstallScanModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelBtnRef = useRef<HTMLButtonElement>(null)

  const titleId = 'install-scan-modal-title'
  const descId = 'install-scan-modal-desc'

  useEffect(() => {
    cancelBtnRef.current?.focus()
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

  return createPortal(
    <>
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
          aria-describedby={descId}
          className="install-scan-modal"
        >
          <div className="mfa-modal__header">
            <h2 id={titleId} className="mfa-modal__title">
              Revisión de seguridad — {name}
            </h2>
            <button
              type="button"
              className="mfa-modal__close"
              aria-label="Cerrar"
              onClick={onCancel}
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>

          <div className="install-scan-modal__body">
            {/* Engine label — makes the scan feel honest, not instant/fake */}
            <div className="install-scan-modal__engine">
              <span className="install-scan-modal__engine-label">Motor de análisis:</span>
              <span className="install-scan-modal__engine-value">{scan.engine_label}</span>
            </div>

            <div className="install-scan-modal__verdict-row">
              <Badge variant={verdictVariant(scan.verdict)}>
                {scan.verdict}
              </Badge>
              <span className="install-scan-modal__score">
                Puntuación: <strong>{scan.score}</strong>/100
              </span>
            </div>

            {scan.risks.length > 0 && (
              <div className="install-scan-modal__risks">
                <p id={descId} className="install-scan-modal__risks-label">Riesgos detectados:</p>
                <ul className="install-scan-modal__risk-list" role="list">
                  {scan.risks.map((r: InstallRisk, i) => (
                    <li key={i} className="install-scan-modal__risk-item">
                      <div className="install-scan-modal__risk-head">
                        <Badge variant={severityVariant(r.severity)}>{r.severity}</Badge>
                        <span className="install-scan-modal__risk-category">{r.category}</span>
                      </div>
                      <p className="install-scan-modal__risk-message">{r.message}</p>
                      {r.evidence_ref && (
                        <p className="install-scan-modal__risk-evidence">{r.evidence_ref}</p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {scan.risks.length === 0 && (
              <p id={descId} className="install-scan-modal__no-risks">
                No se detectaron riesgos específicos.
              </p>
            )}

            <div className="mfa-modal__actions">
              <button
                ref={cancelBtnRef}
                type="button"
                className="cv-btn cv-btn--ghost cv-btn--sm"
                onClick={onCancel}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="cv-btn cv-btn--sm cv-btn--danger cv-btn--danger-solid"
                style={{
                  background: 'var(--danger)',
                  color: '#fff',
                  border: 'none',
                }}
                onClick={() => { void onApprove() }}
              >
                Aprobar e instalar
              </button>
            </div>
          </div>
        </div>
      </div>


    </>,
    document.body,
  )
}
