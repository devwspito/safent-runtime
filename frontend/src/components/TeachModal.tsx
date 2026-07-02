/**
 * TeachModal — full-screen overlay for demonstrating a skill in the real (noVNC) browser.
 * Opened by the "Enseñar habilidad" button in the Habilidades view. Fills the viewport so
 * the jailed browser is as large as possible; Escape or the ✕ closes it.
 */
import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { TeachPanel } from './TeachPanel'

export function TeachModal({
  open,
  onClose,
  onSaved,
}: {
  open: boolean
  onClose: () => void
  onSaved?: () => void
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Enseñar una habilidad"
      style={{
        position: 'fixed', inset: 0, zIndex: 400,
        background: 'var(--color-bg, #0a0a0a)',
        display: 'flex', flexDirection: 'column',
      }}
    >
      <header
        style={{
          display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
          padding: 'var(--space-4) var(--space-5)',
          borderBottom: '1px solid var(--color-border-subtle)',
          flexShrink: 0,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 'var(--text-lg)', fontWeight: 'var(--weight-semibold)' }}>
            Enseñar una habilidad
          </h2>
          <p style={{ margin: '2px 0 0', color: 'var(--color-text-dim)', fontSize: 'var(--text-sm)' }}>
            Ponle nombre, pulsa «Empezar a enseñar» y demuestra la tarea en el navegador. Tus pasos
            se convierten en una habilidad reutilizable.
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Cerrar"
          title="Cerrar (Esc)"
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 34, height: 34, borderRadius: 'var(--radius-md)',
            border: '1px solid var(--color-border-subtle)',
            background: 'var(--color-bg-subtle)', color: 'var(--color-text)', cursor: 'pointer',
            flexShrink: 0,
          }}
        >
          <X size={18} aria-hidden="true" />
        </button>
      </header>

      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', padding: 'var(--space-5)' }}>
        <TeachPanel fullscreen onSaved={onSaved} />
      </div>
    </div>,
    document.body,
  )
}
