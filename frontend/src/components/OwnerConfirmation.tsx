import { useRef, useState, type ReactNode } from 'react'
import { Dialog } from '@base-ui/react/dialog'
import { Button } from './ui/Button'

/** Explicit owner intent, not another login or a standing permission grant. */
export default function OwnerConfirmation({ title, description, onConfirm, onCancel }: {
  title: string
  description?: ReactNode
  onConfirm(): void | Promise<void>
  onCancel(): void
}) {
  const submitted = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function confirm() {
    if (submitted.current) return
    submitted.current = true
    setBusy(true)
    setError('')
    try { await onConfirm() }
    catch { setError('No se pudo completar la acción. Revisa el estado antes de reintentar.') }
    finally { submitted.current = false; setBusy(false) }
  }
  return <Dialog.Root open onOpenChange={open => { if (!open && !submitted.current) onCancel() }}>
    <Dialog.Portal>
      <Dialog.Backdrop className="mfa-modal-backdrop" />
      <Dialog.Popup className="mfa-modal" aria-busy={busy}>
        <div className="mfa-modal__header"><Dialog.Title className="mfa-modal__title">{title}</Dialog.Title></div>
        <div className="mfa-modal__body">
          <Dialog.Description>{description ?? 'Se aplicará el cambio que acabas de revisar. Las demás protecciones permanecen activas.'}</Dialog.Description>
          {error && <p role="alert" className="mfa-modal__inline-error">{error}</p>}
          <div className="mfa-modal__actions">
            <Button disabled={busy} variant="ghost" onClick={onCancel}>Cancelar</Button>
            <Button loading={busy} onClick={() => { void confirm() }}>Confirmar</Button>
          </div>
        </div>
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>
}
