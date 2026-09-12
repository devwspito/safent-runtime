/**
 * AdsView — "Anuncios": the cockpit for Safent Ads campaigns, embedded
 * SAME-ORIGIN (026, contracts/sso.md). The panel loads at `/ads/` through
 * Safent's own session-bridge reverse proxy (T005) — no cross-origin
 * iframe, no `?k=`/token in the src URL: the browser already carries the
 * HttpOnly `ads_bridge` cookie the sidebar's useAdsAvailability poll keeps
 * warm (SC-002: zero-second logins across 20 opens).
 *
 * Every FR-003 availability state gets its own honest empty state with the
 * action that unblocks it — never a blank iframe or a generic error
 * (spec.md Acceptance Scenario 4). "no_accounts" is the one exception: the
 * companion's own panel guides account connection, so Ads stays visible and
 * usable either way (Assumption 7 — never hidden for lack of accounts).
 */
import { useState, type ReactNode } from 'react'
import { Loader2, Megaphone, RefreshCw, ShieldAlert, Wrench, Unplug } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useT } from '../lib/i18n'
import { useAdsAvailability } from '../hooks/useAdsAvailability'
import { Button } from '../components/ui/Button'
import type { AdsAvailabilityReason } from '../api/types'
import css from './AdsView.module.css'
import { ManagedAdsView } from './ManagedAdsView'
import { adsPolicyKey } from '../api/managedAds'

const ADS_IFRAME_SRC = '/ads/'

const BLOCKED_STATE_ICON: Record<AdsAvailabilityReason, ReactNode> = {
  not_installed: <Wrench size={28} aria-hidden="true" />,
  unreachable: <Unplug size={28} aria-hidden="true" />,
  unauthorized: <ShieldAlert size={28} aria-hidden="true" />,
  no_accounts: <Megaphone size={28} aria-hidden="true" />,
}

export default function AdsView() {
  const t = useT()
  const navigate = useNavigate()
  const availability = useAdsAvailability()

  if (availability.status === 'loading') {
    return <AdsState icon={<Loader2 size={24} aria-hidden className="spin" />} title={t('ads.state.loading.title')} loading />
  }

  if (availability.status === 'managed') {
    return availability.policy?.mode === 'managed'
      ? <ManagedAdsView key={adsPolicyKey(availability.policy)} policy={availability.policy}
          refresh={availability.refresh} refreshing={availability.refreshing ?? false} />
      : <AdsState icon={<ShieldAlert size={24} aria-hidden />} title={t('ads.state.unauthorized.title')} />
  }

  const isBlocked = availability.status === 'unavailable' && availability.reason !== 'no_accounts'

  if (isBlocked) {
    const reason = availability.reason ?? 'unreachable'
    return <AdsState
            icon={BLOCKED_STATE_ICON[reason]}
            title={t(`ads.state.${reason}.title`)}
            description={t(`ads.state.${reason}.desc`)}
            action={
              reason === 'unreachable' ? (
                <Button variant="secondary" size="sm" loading={availability.refreshing} onClick={availability.refresh}>
                  <RefreshCw size={13} aria-hidden="true" />
                  {t('ads.state.retry')}
                </Button>
              ) : (
                <Button variant="primary" size="sm" onClick={() => navigate('/capacidades?tab=mcp')}>
                  {t('ads.empty.cta')}
                </Button>
              )
            }
          />
  }

  return <AdsPanel noAccounts={availability.reason === 'no_accounts'} />
}

function AdsState({ icon, title, description, action, loading = false }: {
  icon: ReactNode; title: string; description?: string; action?: ReactNode; loading?: boolean
}) {
  const t = useT()
  return <section className={css.workspace} aria-label={t('nav.ads')}>
    <header className={css.toolbar}><h1><Megaphone size={16} aria-hidden />{t('nav.ads')}</h1></header>
    <div className={css.state} aria-busy={loading}>
      <div className={css.stateIcon}>{icon}</div>
      <h2 role={loading ? 'status' : undefined}>{title}</h2>
      {description && <p>{description}</p>}
      {action && <div>{action}</div>}
    </div>
  </section>
}

function AdsPanel({ noAccounts }: { noAccounts: boolean }) {
  const t = useT()
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<'loading' | 'loaded' | 'error'>('loading')
  return <section className={css.workspace} aria-label={t('nav.ads')}>
    <header className={css.toolbar}>
      <h1><Megaphone size={16} aria-hidden />{t('nav.ads')}</h1>
      <Button size="sm" variant="ghost" onClick={() => { setState('loading'); setRevision(value => value + 1) }}>
        <RefreshCw size={13} aria-hidden />{t('ads.frame.reload')}
      </Button>
    </header>
    {noAccounts && <p className={css.hint}>{t('ads.state.no_accounts.desc')}</p>}
    <div className={css.frameWrap}>
      {state !== 'loaded' && <div className={css.loading} role={state === 'error' ? 'alert' : 'status'}>
        {state === 'loading' ? t('ads.frame.loading') : t('ads.frame.error')}
      </div>}
      <iframe key={revision} src={ADS_IFRAME_SRC} title={t('ads.iframe.title')} className={css.frame}
        onLoad={() => setState('loaded')} onError={() => setState('error')} />
    </div>
  </section>
}
