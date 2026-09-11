import { useRef } from 'react'
import { Dialog } from '@base-ui/react/dialog'
import { X } from 'lucide-react'
import type { SkillDetails } from '../api/types'
import Badge from './Badge'
import { Button } from './ui/Button'
import styles from './SecurityModal.module.css'

/** Untrusted skill instructions are displayed as text, never interpreted. */
export default function SkillDetailsModal({ details, onClose }: { details: SkillDetails; onClose(): void }) {
  const close = useRef<HTMLButtonElement>(null)
  const returnFocus = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog.Root open onOpenChange={open => { if (!open) onClose() }}>
    <Dialog.Portal><Dialog.Backdrop className={styles.backdrop} />
      <Dialog.Popup className={styles.popup} initialFocus={close} finalFocus={returnFocus}>
        <header className={styles.header}><Dialog.Title>{details.skill_name ?? details.package_id}</Dialog.Title><button ref={close} className={styles.close} aria-label="Cerrar" onClick={onClose}><X size={16} aria-hidden /></button></header>
        <Dialog.Description className={styles.description}>Instrucciones de la habilidad instalada. Consultarlas no ejecuta ninguna acción.</Dialog.Description>
        <div className={styles.body}><div className={styles.badges}>{details.version && <Badge variant="neutral">v{details.version}</Badge>}{details.state && <Badge variant="accent">{details.state}</Badge>}</div>
          {details.instructions != null ? <pre className={styles.instructions}>{details.instructions}</pre> : <p className={styles.hint}>Esta habilidad no tiene instrucciones en disco.</p>}
        </div>
        <footer className={styles.footer}><Button variant="secondary" size="sm" onClick={onClose}>Cerrar</Button></footer>
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>
}
