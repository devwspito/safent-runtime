import { useRef, type ReactNode } from 'react'
import { Dialog } from '@base-ui/react/dialog'
import { X } from 'lucide-react'
import { useT } from '../../lib/i18n'
import styles from './Drawer.module.css'

export interface DrawerProps {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
}

/** Base UI owns nested focus, Escape, scroll lock and return to the trigger. */
export function Drawer({ open, title, onClose, children, footer, width = 400 }: DrawerProps) {
  const closeButton = useRef<HTMLButtonElement>(null)
  const t = useT()
  return <Dialog.Root open={open} onOpenChange={value => { if (!value) onClose() }}>
    <Dialog.Portal>
      <Dialog.Backdrop className={styles.backdrop} />
      <Dialog.Popup className={styles.panel} style={{ width }} initialFocus={closeButton}>
        <header className={styles.header}>
          <Dialog.Title className={styles.title}>{title}</Dialog.Title>
          <Dialog.Close ref={closeButton} className="cv-btn cv-btn--ghost cv-btn--sm" aria-label={t('dialog.close')}><X size={16} aria-hidden /></Dialog.Close>
        </header>
        <div className={styles.body}>{children}</div>
        {footer && <footer className={styles.footer}>{footer}</footer>}
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>
}
