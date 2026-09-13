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
import { useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, Loader2, Megaphone, RefreshCw, ShieldAlert, Wrench, Unplug } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useLocale, useT } from '../lib/i18n'
import { useAdsAvailability } from '../hooks/useAdsAvailability'
import { useFeatures } from '../hooks/useFeatures'
import { Button } from '../components/ui/Button'
import type { AdsAvailabilityReason } from '../api/types'
import css from './AdsView.module.css'
import { ManagedAdsView } from './ManagedAdsView'
import { adsPolicyKey } from '../api/managedAds'
import { AdsWorkspaceContext } from '../components/AdsWorkspaceContext'
import { AdsCapsSetup } from '../components/AdsCapsSetup'
import { readAdsCsrf, supportsNativeCaps } from '../lib/adsCaps'

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
  const navigate = useNavigate()
  const [search] = useSearchParams()
  const requestedProvider = search.get('connect')
  const connectProvider = requestedProvider === 'google' || requestedProvider === 'meta' ? requestedProvider : null
  const { locale } = useLocale()
  const features = useFeatures()
  const canConfigureConnections = !features.isLoading && features.edition === 'community' && features.allowed('integraciones')
  const workspace = useContext(AdsWorkspaceContext)
  const setPanelActive = workspace?.setPanelActive
  const returnButton = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLIFrameElement>(null)
  const [showCaps, setShowCaps] = useState(false)
  // The companion can request this one navigation, never an arbitrary URL or
  // a credential operation. Revalidate the actual iframe and current permission.
  useEffect(() => {
    if (!canConfigureConnections) return
    const setup = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || !panelRef.current?.contentWindow || event.source !== panelRef.current.contentWindow) return
      const data: unknown = event.data
      if (!data || typeof data !== 'object' || Array.isArray(data)) return
      const request = data as Record<string, unknown>
      if (Object.keys(request).length !== 2 || request.type !== 'safent:ads-setup' || (request.provider !== 'google' && request.provider !== 'meta')) return
      navigate(`/capacidades?tab=integraciones&ads_setup=${request.provider}`)
    }
    window.addEventListener('message', setup)
    return () => window.removeEventListener('message', setup)
  }, [canConfigureConnections, navigate])
  useLayoutEffect(() => {
    setPanelActive?.(true)
    returnButton.current?.focus()
    return () => setPanelActive?.(false)
  }, [setPanelActive])
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<'loading' | 'loaded' | 'error'>('loading')
  return <section className={css.workspace} aria-label={t('nav.ads')}>
    <header className={css.toolbar}>
      {workspace ? <Button ref={returnButton} size="sm" variant="ghost" className={css.returnButton}
        onClick={workspace.returnToSafent}>
        <ArrowLeft size={15} aria-hidden />{locale === 'en' ? 'Back to Safent' : 'Volver a Safent'}
      </Button> : <h1><Megaphone size={16} aria-hidden />{t('nav.ads')}</h1>}
      <Button size="sm" variant="ghost" onClick={() => { setState('loading'); setRevision(value => value + 1) }}>
        <RefreshCw size={13} aria-hidden />{t('ads.frame.reload')}
      </Button>
      {canConfigureConnections && supportsNativeCaps() && <Button size="sm" variant="secondary" onClick={() => setShowCaps(value => !value)}>{locale === 'en' ? 'Spending limits' : 'Límites de gasto'}</Button>}
    </header>
    {canConfigureConnections && <div className={`${css.hint} ${css.connectionsSetup}`}>
      <span>{t('ads.connections.setup_hint')}</span>
      <Link className="cv-btn cv-btn--ghost cv-btn--sm" to="/capacidades?tab=integraciones">{t('ads.connections.setup_action')}</Link>
    </div>}
    {noAccounts && <p className={css.hint}>{t('ads.state.no_accounts.desc')}</p>}
    {showCaps && canConfigureConnections && <AdsCapsSetup getCsrf={() => readAdsCsrf(panelRef.current)} onClose={() => setShowCaps(false)} />}
    <div className={css.frameWrap}>
      {state !== 'loaded' && <div className={css.loading} role={state === 'error' ? 'alert' : 'status'}>
        {state === 'loading' ? t('ads.frame.loading') : t('ads.frame.error')}
      </div>}
      <iframe ref={panelRef} key={`${revision}:${connectProvider ?? ''}:${canConfigureConnections}`}
        src={connectProvider ? `/ads/conexiones?provider=${connectProvider}` : ADS_IFRAME_SRC}
        data-safent-setup={canConfigureConnections ? 'true' : undefined}
        title={t('ads.iframe.title')} className={css.frame}
        onLoad={() => setState('loaded')} onError={() => setState('error')} />
    </div>
  </section>
}
