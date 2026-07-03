import type { ReactNode } from 'react'
import styles from './Tabs.module.css'

export interface Tab {
  key: string
  label: string
  /** Optional badge count displayed next to the label. */
  count?: number
  /** Needs-your-attention count — rendered as the same red badge as the sidebar. */
  alertCount?: number
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
  return (
    <nav className={styles.tabs} role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab) => (
        <button
          key={tab.key}
          role="tab"
          aria-selected={active === tab.key}
          className={styles.tab}
          onClick={() => onChange(tab.key)}
          type="button"
        >
          {tab.label}
          {tab.count != null ? (
            <span aria-label={`${tab.count} elementos`} style={{ marginLeft: 6 }}>
              {tab.count}
            </span>
          ) : null}
          {tab.alertCount != null && tab.alertCount > 0 ? (
            <span className="badge-count" role="status" style={{ marginLeft: 6 }}>
              {tab.alertCount}
            </span>
          ) : null}
        </button>
      ))}
      {trailing ? <div style={{ marginLeft: 'auto' }}>{trailing}</div> : null}
    </nav>
  )
}
