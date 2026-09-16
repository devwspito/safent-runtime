import { useRef, useState } from 'react'
import { Dialog } from '@base-ui/react/dialog'
import { ShieldCheck, X } from 'lucide-react'
import type { InstallScanResponse } from '../api/types'
import Badge, { type BadgeVariant } from './Badge'
import { Button } from './ui/Button'
import styles from './SecurityModal.module.css'

const severity = (value: string): BadgeVariant => ['critical', 'high'].includes(value.toLowerCase()) ? 'danger' : value.toLowerCase() === 'medium' ? 'warn' : 'neutral'

/** Reviewing a scan does not bypass the server's install/approval gate. */
export default function InstallScanModal({ scan, name, onApprove, onCancel }: {
  scan: InstallScanResponse; name: string; onApprove(): void | Promise<void>; onCancel(): void
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  const returnFocus = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  const submitted = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  async function approve() {
    if (submitted.current) return
    submitted.current = true; setBusy(true); setError(false)
    try { await onApprove() }
    catch { setError(true) }
    finally { submitted.current = false; setBusy(false) }
  }
  return <Dialog.Root open onOpenChange={open => { if (!open && !submitted.current) onCancel() }}>
    <Dialog.Portal><Dialog.Backdrop className={styles.backdrop} />
      <Dialog.Popup className={styles.popup} initialFocus={cancel} finalFocus={returnFocus} aria-busy={busy}>
        <header className={styles.header}><ShieldCheck size={18} aria-hidden /><Dialog.Title>Revisión de seguridad</Dialog.Title><button className={styles.close} aria-label="Cerrar" disabled={busy} onClick={onCancel}><X size={16} aria-hidden /></button></header>
        <Dialog.Description className={styles.description}>Revisa los riesgos antes de instalar {name}. La decisión se limita a esta instalación.</Dialog.Description>
        <div className={styles.body}>
          <dl className={styles.metadata}><div><dt>Motor de análisis</dt><dd>{scan.engine_label}</dd></div><div><dt>Resultado</dt><dd><Badge variant={scan.verdict === 'PASS' ? 'ok' : scan.verdict === 'WARN' ? 'warn' : 'danger'}>{scan.verdict}</Badge></dd></div><div><dt>Puntuación</dt><dd>{scan.score}/100</dd></div></dl>
          {scan.risks.length ? <ul className={styles.risks} aria-label="Riesgos detectados">{scan.risks.map((risk, index) => <li key={index}><div className={styles.riskTitle}><Badge variant={severity(risk.severity)}>{risk.severity}</Badge><strong>{risk.category}</strong></div><p>{risk.message}</p>{risk.evidence_ref && <code>{risk.evidence_ref}</code>}</li>)}</ul> : <p className={styles.hint}>No se detectaron riesgos específicos.</p>}
          {error && <p role="alert" className={styles.error}>No se pudo completar la solicitud. Comprueba el estado antes de reintentar.</p>}
        </div>
        <footer className={styles.footer}><Button ref={cancel} variant="ghost" size="sm" disabled={busy} onClick={onCancel}>Cancelar</Button><Button variant="danger" size="sm" loading={busy} disabled={busy} onClick={() => void approve()}>Aprobar e instalar</Button></footer>
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>
}
