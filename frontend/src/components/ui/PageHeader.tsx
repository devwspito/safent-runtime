import type { ReactNode } from 'react'

export interface PageHeaderProps {
  title: string
  subtitle?: string
  /** Trailing controls rendered flush right. */
  actions?: ReactNode
}

export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <header className="view-header ds-page-header">
      <div className="ds-page-header__left">
        <h1 className="view-title">{title}</h1>
        {subtitle ? <p className="view-subtitle">{subtitle}</p> : null}
      </div>
      {actions ? <div className="ds-page-header__actions">{actions}</div> : null}
    </header>
  )
}
