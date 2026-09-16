import { useRef, type ReactNode } from 'react'
import styles from './Tabs.module.css'

export interface Tab {
  key: string
  label: string
  /** Optional badge count displayed next to the label. */
  count?: number
  /** Needs-your-attention count — rendered as the same red badge as the sidebar. */
  alertCount?: number
  /** Optional relationship to the panel owned by the caller. */
  panelId?: string
}

export interface TabsProps {
  tabs: Tab[]
  active: string
  onChange: (key: string) => void
  /** Accessible label for the tablist. Defaults to "Vista". */
  ariaLabel?: string
  /** Optional trailing content (e.g. search input, button). */
  trailing?: ReactNode
}

export function Tabs({ tabs, active, onChange, ariaLabel = 'Vista', trailing }: TabsProps) {
  const buttons = useRef(new Map<string, HTMLButtonElement>())
  const focusable = tabs.some(tab => tab.key === active) ? active : tabs[0]?.key

  return (
    <div className={styles.tabs}>
      <div className={styles.tabList} role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab, index) => (
        <button
          key={tab.key}
          ref={node => {
            if (node) buttons.current.set(tab.key, node)
            else buttons.current.delete(tab.key)
          }}
          role="tab"
          aria-selected={active === tab.key}
          aria-controls={tab.panelId}
          tabIndex={focusable === tab.key ? 0 : -1}
          className={styles.tab}
          onClick={() => onChange(tab.key)}
          onKeyDown={event => {
            const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length
              : event.key === 'ArrowLeft' ? (index - 1 + tabs.length) % tabs.length
              : event.key === 'Home' ? 0
              : event.key === 'End' ? tabs.length - 1
              : null
            if (next === null) return
            event.preventDefault()
            // Manual activation: exploring tabs must not mount views or fetch.
            // Native Enter/Space activates the focused button immediately.
            buttons.current.get(tabs[next].key)?.focus()
          }}
          type="button"
        >
          {tab.label}
          {tab.count != null ? (
            <span className={styles.count} aria-label={`${tab.count} elementos`}>
              {tab.count}
            </span>
          ) : null}
          {tab.alertCount != null && tab.alertCount > 0 ? (
            <span className="badge-count" role="status">
              {tab.alertCount}
            </span>
          ) : null}
        </button>
      ))}
      </div>
      {trailing ? <div className={styles.trailing}>{trailing}</div> : null}
    </div>
  )
}
