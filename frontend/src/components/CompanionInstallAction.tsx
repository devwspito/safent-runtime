/**
 * CompanionInstallAction — the ONE "Instalar"/"Reparar" affordance for the
 * Ads companion (029 FR-001), shared by the Herramientas card (McpView) and
 * the sidebar's `not_installed` entry (AdsNavItem). Renders nothing while
 * ready/loading or for reasons it can't act on (unauthorized/no_accounts —
 * the companion's own onboarding panel owns those). Never renders a fake
 * "conectado": progress and failure come straight from the install-request
 * readback (useCompanionInstall).
 */
import { Loader2 } from 'lucide-react'
import { useT } from '../lib/i18n'
import { stageLabelKey, formatProgress } from '../lib/installStages'
import { useCompanionInstall } from '../hooks/useCompanionInstall'
import type { AdsAvailability } from '../hooks/useAdsAvailability'
import { Button } from './ui/Button'

export interface CompanionInstallActionProps {
  availability: AdsAvailability
  /** Compact rendering for the sidebar nav item — action only, no description line. */
  compact?: boolean
}

export function CompanionInstallAction({ availability, compact = false }: CompanionInstallActionProps) {
  const t = useT()
  const { phase, install, repair, retry, refresh } = useCompanionInstall(availability)

  if (phase.kind === 'hidden') return null
  if (phase.kind === 'checking') return <span role="status">{t('sysupdate.checking')}</span>
  if (phase.kind === 'unavailable') return <div>
    {!compact && <p role="status">{t('ads.install.unknown')}</p>}
    <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm" title={t('ads.install.unknown')} onClick={refresh}>{t('sysupdate.check_again')}</button>
  </div>

  if (phase.kind === 'installing') {
    const stageText = t(stageLabelKey(phase.stage))
    const progressText = formatProgress(phase.progress)
    return (
      <span
        aria-live="polite"
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          fontSize: compact ? 'var(--text-xs)' : undefined,
          color: 'var(--color-text-dim)',
        }}
      >
        <Loader2 size={compact ? 12 : 14} className="spin" aria-hidden="true" />
        <span>{stageText}{progressText && ` (${progressText})`}</span>
      </span>
    )
  }

  const isProblem = phase.kind === 'expired' || phase.kind === 'failed'
  const label =
    phase.kind === 'install' ? t('ads.install.action') :
    phase.kind === 'repair' ? t('ads.install.repair_action') :
    t('ads.state.retry')
  const onAction = phase.kind === 'install' ? install : phase.kind === 'repair' ? repair : retry
  const description =
    phase.kind === 'expired' ? t('ads.install.expired') :
    phase.kind === 'failed' ? (phase.label || t('ads.install.err.generic')) :
    null

  if (compact) {
    return (
      <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm" onClick={onAction}>
        {label}
      </button>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      {description && (
        <p role={isProblem ? 'alert' : undefined} style={{ margin: 0, fontSize: 'var(--text-xs)', color: 'var(--color-text-dim)' }}>
          {description}
        </p>
      )}
      <Button variant="primary" size="sm" type="button" onClick={onAction}>
        {label}
      </Button>
    </div>
  )
}
