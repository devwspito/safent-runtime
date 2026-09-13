import { useEffect, useId, useReducer, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { sileo } from 'sileo'
import { Check, Plug, Globe, RefreshCw, Search } from 'lucide-react'
import { useLocale, useT, type TranslationKey } from '../lib/i18n'
import { getAuthStatus, subscribeAuthStatus } from '../lib/token'
import { composioAppName } from '../lib/composio'
import {
  getComposioStatus, listComposioConnected, listComposioApps,
  connectComposioApp, setComposioApiKey, setupComposioMeta,
  getWebSearchStatus, setWebSearchKey,
  ApiError,
} from '../api/client'
import type { ComposioStatus, ComposioApp, ComposioConnectedAccount, WebSearchStatus } from '../api/types'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import styles from './IntegrationsView.module.css'
import { EnterpriseCrm } from './EnterpriseCrm'

// Mirrors vanilla integrations.js load order: status first → prevents calling
// connected/apps when Composio has no key (avoids hanging for minutes).

type ComposioState =
  | { status: 'loading' }
  | { status: 'no-key' }
  | { status: 'error'; message: string }
  | { status: 'ready'; info: ComposioStatus; connected: ComposioConnectedAccount[]; apps: ComposioApp[]; connectedError: boolean; appsError: boolean }

type ComposioAction =
  | { type: 'LOADING' }
  | { type: 'NO_KEY' }
  | { type: 'FAILED'; message: string }
  | { type: 'READY'; info: ComposioStatus; connected: ComposioConnectedAccount[]; apps: ComposioApp[]; connectedError: boolean; appsError: boolean }

function composioReducer(_s: ComposioState, a: ComposioAction): ComposioState {
  switch (a.type) {
    case 'LOADING': return _s.status === 'ready' ? _s : { status: 'loading' }
    case 'NO_KEY': return { status: 'no-key' }
    case 'FAILED': return { status: 'error', message: a.message }
    case 'READY': return { status: 'ready', info: a.info, connected: a.connected, apps: a.apps, connectedError: a.connectedError, appsError: a.appsError }
  }
}

// Web-search — separate state machine; lightweight enough to stay in useState
type WsState =
  | { status: 'loading' }
  | { status: 'ready'; data: WebSearchStatus }
  | { status: 'error'; message: string }

function show(message: string, kind: 'ok' | 'warn' | 'error' | 'info' = 'ok') {
  if (kind === 'ok') sileo.success({ title: message })
  else if (kind === 'error') sileo.error({ title: message })
  else if (kind === 'warn') sileo.warning({ title: message })
  else sileo.info({ title: message })
}

// ── Skeleton grid — mirrors the final app-card layout ────────────────────────

function AppGridSkeleton() {
  const t = useT()
  return (
    <div className={styles.skeletonGrid} aria-busy="true" aria-label={t('int.loading_apps_aria')}>
      {Array.from({ length: 3 }, (_, i) => (
        <div
          key={i}
          className={`skeleton skeleton--card ${styles.skeletonCard}`}
          aria-hidden="true"
        />
      ))}
    </div>
  )
}

export default function IntegrationsView() {
  const t = useT()
  const [composioState, dispatch] = useReducer(composioReducer, { status: 'loading' })
  const [wsState, setWsState] = useState<WsState>({ status: 'loading' })
  const reloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const composioRevision = useRef(0)
  const webRevision = useRef(0)
  const [refreshing, setRefreshing] = useState(false)
  const [query, setQuery] = useState('')

  // Clear the reload timer on unmount so it never fires on a dead component
  useEffect(() => {
    return () => {
      composioRevision.current++
      webRevision.current++
      if (reloadTimerRef.current !== null) clearTimeout(reloadTimerRef.current)
    }
  }, [])

  // Refetch when the user returns to this tab (e.g. after completing the OAuth
  // flow in the popup/new tab). A fixed 3s delay almost always fired before the
  // user finished authorizing, so a just-connected app looked "not connected".
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const onFocus = () => { loadComposio() }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [])

  async function loadComposio() {
    const revision = ++composioRevision.current
    setRefreshing(true)
    dispatch({ type: 'LOADING' })
    let status: ComposioStatus
    try {
      status = await getComposioStatus()
    } catch (e) {
      if (revision !== composioRevision.current) return
      setRefreshing(false)
      dispatch({
        type: 'FAILED',
        message: e instanceof ApiError ? e.message : t('int.err.composio'),
      })
      return
    }

    if (revision !== composioRevision.current) return
    if (!status.has_key) {
      setRefreshing(false)
      dispatch({ type: 'NO_KEY' })
      return
    }

    const [connected, apps] = await Promise.allSettled([
      listComposioConnected(),
      listComposioApps(),
    ])
    if (revision !== composioRevision.current) return
    setRefreshing(false)
    const validApps = (result: PromiseSettledResult<ComposioApp[]>): result is PromiseFulfilledResult<ComposioApp[]> => result.status === 'fulfilled' && Array.isArray(result.value) && result.value.every(app => app && typeof app.slug === 'string' && app.slug.length > 0)
    dispatch({
      type: 'READY',
      info: status,
      connected: connected.status === 'fulfilled' ? connected.value : [],
      apps: validApps(apps) ? apps.value : [],
      connectedError: connected.status === 'rejected',
      appsError: !validApps(apps),
    })
  }

  async function loadWebSearch() {
    const revision = ++webRevision.current
    setWsState({ status: 'loading' })
    try {
      const st = await getWebSearchStatus()
      if (revision !== webRevision.current) return
      setWsState({ status: 'ready', data: st })
    } catch (e) {
      if (revision !== webRevision.current) return
      setWsState({
        status: 'error',
        message: e instanceof ApiError ? e.message : t('int.err.websearch'),
      })
    }
  }

  useEffect(() => {
    loadComposio()
    loadWebSearch()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const connectedSlugs = composioState.status === 'ready'
    ? new Set(composioState.connected.filter(c => c.status === 'ACTIVE').map(c => c.toolkit_slug))
    : new Set<string>()

  return (
    <>
      <PageHeader
        title={t('view.integraciones')}
        subtitle={t('int.subtitle')}
        actions={<Button variant="ghost" size="sm" disabled={refreshing} onClick={() => void loadComposio()} aria-label={t('int.refresh')}><RefreshCw size={14} aria-hidden />{refreshing ? t('int.refreshing') : t('int.refresh')}</Button>}
      />

      <div className={`view-body ${styles.body}`}>
          <EnterpriseCrm />

          {/* ── Web search (Brave) ─────────────────────────────────────────── */}
            <section className={styles.section} aria-label={t('int.websearch.label')}>
              <h2 className={styles.sectionLabel}>{t('int.websearch.label')}</h2>

              {wsState.status === 'loading' && (
                <div
                  className="skeleton skeleton--block"
                  style={{ height: 56, borderRadius: 'var(--radius-md)' }}
                  aria-busy="true"
                  aria-label={t('int.websearch.loading_aria')}
                />
              )}

              {wsState.status === 'error' && (
                  <div role="alert" className={styles.errorRow}>
                    <p className={styles.errorText}>{wsState.message}</p>
                    <Button variant="secondary" size="sm" onClick={loadWebSearch}>
                      {t('int.retry')}
                    </Button>
                  </div>
              )}

              {wsState.status === 'ready' && (
                <WebSearchCard
                  status={wsState.data}
                  onSaved={() => { loadWebSearch(); show(t('int.brave.activated_toast'), 'ok') }}
                  onToast={show}
                />
              )}
            </section>

          {/* ── Composio connection status ────────────────────────────────── */}
            <section className={styles.section} aria-label={t('int.connected_services.aria')}>
              <h2 className={styles.sectionLabel}>{t('int.connect_apps')}</h2>

              {composioState.status === 'loading' && (
                <div
                  className="skeleton skeleton--block"
                  style={{ height: 48, borderRadius: 'var(--radius-md)' }}
                  aria-busy="true"
                  aria-label={t('int.composio.checking_aria')}
                />
              )}

              {composioState.status === 'no-key' && (
                <ComposioSetupCard
                  onSaved={() => {
                    loadComposio()
                    show(t('int.composio.connected_toast'), 'ok')
                  }}
                  onToast={show}
                />
              )}

              {composioState.status === 'error' && (
                  <div role="alert" className={styles.errorRow}>
                    <p className={styles.errorText}>{composioState.message}</p>
                    <Button variant="secondary" size="sm" onClick={loadComposio}>
                      {t('int.retry')}
                    </Button>
                  </div>
              )}

              {composioState.status === 'ready' && (
                  <div className={styles.statusBanner} aria-label={t('int.composio.active_aria')}>
                    <Check
                      size={14}
                      className={styles.statusBannerCheck}
                      aria-hidden="true"
                    />
                    <span>
                      {t('int.composio.active_account')}{' '}
                      <code className={styles.statusBannerCode}>
                        {composioState.info.entity_id ?? '—'}
                      </code>
                    </span>
                  </div>
              )}
              {composioState.status === 'ready' && <MetaSetupCard />}
              <p className={styles.adsNote}>
                {t('int.ads.guidance')}{' '}
                <Link className={styles.adsLink} to="/anuncios">{t('int.ads.open')}</Link>
              </p>
            </section>

          {/* ── Connected apps ────────────────────────────────────────────── */}
            <section className={styles.section} aria-label={t('int.connected_apps.aria')}>
              <h2 className={styles.sectionLabel}>{t('int.connected_apps.label')}</h2>

              {composioState.status === 'loading' && <AppGridSkeleton />}

              {(composioState.status === 'no-key' || composioState.status === 'error') && (
                <p className={styles.lockedPlaceholder}>
                  {t('int.locked.connected')}
                </p>
              )}

              {composioState.status === 'ready' && (
                composioState.connectedError
                  ? <SourceError message={t('int.err.connected')} onRetry={loadComposio} busy={refreshing} />
                  : composioState.connected.length === 0
                  ? (
                    <EmptyState
                      compact
                      icon={<Plug size={28} />}
                      title={t('int.empty.connected.title')}
                      description={t('int.empty.connected.desc')}
                    />
                  )
                  : (
                    <ul className={styles.appGrid} role="list">
                        {composioState.connected.map(account => (
                          <li key={account.id}>
                            <AppCard
                              app={composioState.apps.find(app => app.slug === account.toolkit_slug) ?? { slug: account.toolkit_slug }}
                              connection={account}
                              showConnectionId={composioState.connected.filter(other => other.toolkit_slug === account.toolkit_slug).length > 1}
                            />
                          </li>
                        ))}
                    </ul>
                  )
              )}
            </section>

          {/* ── Available apps ────────────────────────────────────────────── */}
            <section className={styles.section} aria-label={t('int.available_apps.aria')}>
              <div className={styles.catalogHeader}>
                <h2 className={styles.sectionLabel}>{t('int.catalog.label')}</h2>
                <label className={styles.searchField}>
                  <Search size={14} aria-hidden />
                  <input value={query} onChange={event => setQuery(event.target.value)} placeholder={t('int.search')} aria-label={t('int.search')} />
                </label>
              </div>

              {composioState.status === 'loading' && <AppGridSkeleton />}

              {(composioState.status === 'no-key' || composioState.status === 'error') && (
                <p className={styles.lockedPlaceholder}>
                  {t('int.locked.catalog')}
                </p>
              )}

              {composioState.status === 'ready' && (() => {
                if (composioState.appsError) return <SourceError message={t('int.err.catalog')} onRetry={loadComposio} busy={refreshing} />
                const remaining = composioState.apps.filter(a => !connectedSlugs.has(a.slug) && `${a.name ?? ''} ${a.slug} ${a.description ?? ''}`.toLowerCase().includes(query.trim().toLowerCase()))
                return remaining.length === 0
                  ? (
                    <EmptyState
                      icon={<Globe size={28} />}
                      compact
                      title={t(query.trim() ? 'int.search.empty' : 'int.catalog.empty')}
                    />
                  )
                  : (
                    <ul className={styles.appGrid} role="list">
                        {remaining.map(app => (
                          <li key={app.slug}>
                            <AppCard
                              app={app}
                              disabled={composioState.connectedError || refreshing}
                              onConnect={async (a) => {
                                try {
                                  const r = await connectComposioApp(a.slug)
                                  if (r?.redirect_url) {
                                    window.open(r.redirect_url, '_blank', 'noopener,noreferrer')
                                  }
                                  show(t('int.connecting_app_toast').replace('{name}', composioAppName(a)), 'info')
                                  if (reloadTimerRef.current !== null) clearTimeout(reloadTimerRef.current)
                                  reloadTimerRef.current = setTimeout(loadComposio, 3000)
                                } catch (e) {
                                  show(e instanceof Error ? e.message : t('int.err.generic'), 'error')
                                }
                              }}
                            />
                          </li>
                        ))}
                    </ul>
                  )
              })()}
            </section>
      </div>
    </>
  )
}

// ── App card (grid item) ──────────────────────────────────────────────────────

interface AppCardProps {
  app: ComposioApp
  connection?: ComposioConnectedAccount
  showConnectionId?: boolean
  disabled?: boolean
  onConnect?: (app: ComposioApp) => void | Promise<void>
}

function SourceError({ message, onRetry, busy }: { message: string; onRetry: () => void; busy: boolean }) {
  const t = useT()
  return <div role="alert" className={styles.errorRow}><p className={styles.errorText}>{message}</p><Button variant="secondary" size="sm" onClick={onRetry} disabled={busy}>{t('int.retry')}</Button></div>
}

function connectionStatusKey(status: string): TranslationKey {
  switch (status) {
    case 'ACTIVE': return 'int.connected_badge'
    case 'INITIATED': case 'INITIALIZING': return 'int.connection.pending'
    case 'EXPIRED': return 'int.connection.expired'
    case 'FAILED': return 'int.connection.failed'
    case 'INACTIVE': case 'REVOKED': return 'int.connection.inactive'
    default: return 'int.connection.unknown'
  }
}

function AppCard({ app, connection, showConnectionId, onConnect, disabled }: AppCardProps) {
  const t = useT()
  const [connecting, setConnecting] = useState(false)
  const displayName = composioAppName(app)
  const isConnected = connection?.status === 'ACTIVE'
  const isAds = app.slug === 'googleads' || app.slug === 'metaads'

  const cardClass = [
    styles.appCard,
    isConnected ? styles.appCardConnected : '',
  ].filter(Boolean).join(' ')

  return (
    <div className={cardClass}>
      <div className={styles.appIconWrap} aria-hidden="true">
        {app.logo
          ? <img src={app.logo} alt="" width={20} height={20} />
          : <Plug size={14} className={styles.appIconFallback} />
        }
      </div>

      <div className={styles.appInfo}>
        <div className={styles.appName}>{displayName}</div>
        {connection && showConnectionId && (
          <div className={styles.appDesc} title={connection.id}>
            {t('int.connection.label').replace('{id}', connection.id)}
          </div>
        )}
        {app.description && (
          <div className={styles.appDesc} title={app.description}>
            {app.description}
          </div>
        )}
      </div>

      <div className={styles.appAction}>
        {connection
          ? (
            <span className={isConnected ? styles.connectedBadge : styles.connectionStatus}>
              {isConnected && <Check size={10} aria-hidden="true" />}
              {t(connectionStatusKey(connection.status))}
            </span>
          )
          : isAds ? null
          : (
            <Button
              variant="ghost"
              size="sm"
              aria-label={t('int.connect_aria').replace('{name}', displayName)}
              disabled={disabled || connecting}
              loading={connecting}
              onClick={async () => {
                if (connecting || disabled) return
                setConnecting(true)
                try { await onConnect?.(app) } finally { setConnecting(false) }
              }}
            >
              {t('int.connect_btn')}
            </Button>
          )
        }
        {isAds && (
          <Link className={styles.adsLink} to="/anuncios" aria-label={t('int.ads.open_aria').replace('{name}', displayName)}>
            {t('int.ads.open')}
          </Link>
        )}
      </div>
    </div>
  )
}

// ── Composio setup card (no key yet) ─────────────────────────────────────────

interface ComposioSetupCardProps {
  onSaved: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function ComposioSetupCard({ onSaved, onToast }: ComposioSetupCardProps) {
  const t = useT()
  const [saving, setSaving] = useState(false)
  const keyRef = useRef<HTMLInputElement>(null)

  async function handleSave() {
    if (saving) return
    const key = keyRef.current?.value.trim() ?? ''
    if (!key) { onToast(t('int.err.enter_key'), 'warn'); return }
    setSaving(true)
    try {
      await setComposioApiKey(key)
      if (keyRef.current) keyRef.current.value = ''
      onSaved()
    } catch (e) {
      onToast(e instanceof Error ? e.message : t('int.err.generic'), 'error')
    } finally { setSaving(false) }
  }

  return (
    <div className={styles.setupCard}>
      <p className={styles.setupCardTitle}>{t('int.composio.setup.title')}</p>
      <p className={styles.setupCardBody}>
        {t('int.composio.setup.body')}
      </p>
      <details className={styles.setupCardSteps}><summary>{t('int.setup.help')}</summary><p>
        {t('int.composio.setup.step1')}{' '}
        <a href="https://app.composio.dev/developers" target="_blank" rel="noopener noreferrer">
          app.composio.dev
        </a>
        {'  ·  '}{t('int.composio.setup.step2')}{'  ·  '}{t('int.composio.setup.step3_pre')}{' '}
        <strong>Settings → API Keys</strong> {t('int.composio.setup.step3_post')}
      </p></details>
      <div className={styles.formInline}>
        <label className="sr-only" htmlFor="composio-apikey">{t('int.access_key.label')}</label>
        {/* Secret: password input, never echoed back */}
        <input
          id="composio-apikey"
          ref={keyRef}
          className={styles.keyInput}
          type="password"
          placeholder={t('int.access_key.placeholder')}
          autoComplete="new-password"
          onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) void handleSave() }}
        />
        <Button
          variant="primary"
          size="sm"
          onClick={handleSave}
          disabled={saving}
          loading={saving}
        >
          {saving ? t('int.connecting') : t('int.connect_btn')}
        </Button>
      </div>
    </div>
  )
}

// ── Owner preparation for Meta Ads (separate from account authorization) ──────

const META_CALLBACK_URL = 'https://backend.composio.dev/api/v1/auth-apps/add'

// Local copy keeps this small setup surface independent of the shared catalog.
const META_SETUP_COPY = {
  es: {
    title: 'Preparar Meta Ads en Composio',
    help: 'Configuración inicial: quien administra la app de Meta debe añadir esta dirección de retorno a su configuración de acceso (OAuth).',
    developerLink: 'Abrir Meta for Developers',
    callback: 'Dirección de retorno de Meta',
    copy: 'Copiar dirección',
    copied: 'Dirección copiada.',
    copyError: 'No se pudo copiar. Selecciona la dirección de retorno y cópiala manualmente.',
    appId: 'Identificador de la app (App ID)',
    appSecret: 'Clave de la app (App Secret)',
    privacy: 'La clave de la app (App Secret) se guarda en Composio, no en el navegador. Preparar la app no autoriza ninguna cuenta publicitaria.',
    prepare: 'Preparar Meta',
    preparing: 'Preparando Meta…',
    invalid: 'Introduce el identificador numérico de la app (de 5 a 30 dígitos) y su clave.',
    failed: 'No se pudo preparar Meta. Revisa la app, su identificador y clave y la conexión de Composio; vuelve a introducir la clave de la app para reintentar.',
    session: 'Tu sesión ha caducado. Vuelve a abrir Safent antes de preparar Meta.',
    owner: 'Sólo el propietario de Safent puede preparar Meta. Revisa tu sesión.',
    limited: 'Hay demasiados intentos. Espera un momento antes de volver a preparar Meta.',
    ready: 'Meta está preparada en Composio. Todavía debes autorizar cada cuenta publicitaria en Anuncios.',
    ads: 'Ir a Anuncios',
  },
  en: {
    title: 'Prepare Meta Ads in Composio',
    help: 'One-time setup: the Meta app administrator must add this callback to its OAuth settings.',
    developerLink: 'Open Meta for Developers',
    callback: 'Meta callback',
    copy: 'Copy callback',
    copied: 'Callback copied.',
    copyError: 'Could not copy. Select the callback and copy it manually.',
    appId: 'App ID',
    appSecret: 'App Secret',
    privacy: 'The App Secret is stored in Composio, not in the browser. Preparing the app does not authorize any advertising account.',
    prepare: 'Prepare Meta',
    preparing: 'Preparing Meta…',
    invalid: 'Enter your Meta app’s numeric App ID (5 to 30 digits) and App Secret.',
    failed: 'Could not prepare Meta. Check the app, its credentials and the Composio connection; enter the App Secret again to retry.',
    session: 'Your session expired. Reopen Safent before preparing Meta.',
    owner: 'Only the Safent owner can prepare Meta. Check your session.',
    limited: 'Too many attempts. Wait a moment before preparing Meta again.',
    ready: 'Meta is prepared in Composio. You still need to authorize each advertising account in Ads.',
    ads: 'Go to Ads',
  },
} as const

function MetaSetupCard() {
  // Safent is single-owner: use the existing session gate, never infer a role
  // from form data. The endpoint enforces the owner session independently.
  const auth = useSyncExternalStore(subscribeAuthStatus, getAuthStatus)
  return auth.kind === 'authenticated' ? <MetaSetupForm /> : null
}

function MetaSetupForm() {
  const { locale } = useLocale()
  const copy = META_SETUP_COPY[locale]
  const id = useId()
  const clientRef = useRef<HTMLInputElement>(null)
  const secretRef = useRef<HTMLInputElement>(null)
  const callbackRef = useRef<HTMLInputElement>(null)
  const inFlight = useRef(false)
  const generation = useRef(0)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<'invalid' | 'failed' | 'session' | 'owner' | 'limited' | 'ready' | null>(null)
  const [copyFeedback, setCopyFeedback] = useState<'copied' | 'copyError' | null>(null)

  useEffect(() => {
    generation.current++
    return () => { generation.current++ }
  }, [])

  async function prepare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (inFlight.current || getAuthStatus().kind !== 'authenticated') return
    const clientId = clientRef.current?.value.trim() ?? ''
    const clientSecret = secretRef.current?.value.trim() ?? ''
    if (!/^[0-9]{5,30}$/.test(clientId) || !clientSecret || clientSecret.length > 4096) {
      setFeedback('invalid')
      return
    }
    const current = generation.current
    inFlight.current = true
    setSaving(true)
    setFeedback(null)
    // Keep secrets out of React state and browser persistence. Clear the field
    // before sending, including when the server rejects this attempt.
    if (secretRef.current) secretRef.current.value = ''
    try {
      await setupComposioMeta({ client_id: clientId, client_secret: clientSecret })
      if (generation.current === current) setFeedback('ready')
    } catch (failure) {
      if (generation.current !== current) return
      const status = failure instanceof ApiError ? failure.status : 0
      setFeedback(status === 401 ? 'session' : status === 403 ? 'owner' : status === 429 ? 'limited' : 'failed')
    } finally {
      if (generation.current === current) {
        inFlight.current = false
        setSaving(false)
      }
    }
  }

  async function copyCallback() {
    const current = generation.current
    try {
      await navigator.clipboard.writeText(META_CALLBACK_URL)
      if (generation.current === current) setCopyFeedback('copied')
    } catch {
      if (generation.current !== current) return
      setCopyFeedback('copyError')
      callbackRef.current?.focus()
      callbackRef.current?.select()
    }
  }

  return (
    <form className={styles.setupCard} aria-labelledby={`${id}-title`} onSubmit={event => void prepare(event)} noValidate>
      <h3 id={`${id}-title`} className={styles.setupCardTitle}>{copy.title}</h3>
      <p className={styles.setupCardBody}>{copy.help}{' '}
        <a className={styles.adsLink} href="https://developers.facebook.com/apps/" target="_blank" rel="noopener noreferrer">{copy.developerLink}</a>
      </p>
      <div className={styles.metaCallback}>
        <label htmlFor={`${id}-callback`}>{copy.callback}</label>
        <div className={styles.formInline}>
          <input ref={callbackRef} id={`${id}-callback`} className={styles.keyInput} value={META_CALLBACK_URL} readOnly spellCheck={false} />
          <Button type="button" variant="secondary" size="sm" onClick={() => void copyCallback()}>{copy.copy}</Button>
        </div>
        {copyFeedback && <p className={styles.setupCardBody} role="status">{copy[copyFeedback]}</p>}
      </div>
      <p id={`${id}-privacy`} className={styles.setupCardBody}>{copy.privacy}</p>
      <div className={styles.metaFields}>
        <label className={styles.metaField} htmlFor={`${id}-client`}>{copy.appId}
          <input ref={clientRef} id={`${id}-client`} className={styles.keyInput} type="text" inputMode="numeric" autoComplete="off" spellCheck={false}
            required disabled={saving} maxLength={30} aria-invalid={feedback === 'invalid' || undefined} aria-describedby={`${id}-privacy`}
            onChange={() => setFeedback(null)} />
        </label>
        <label className={styles.metaField} htmlFor={`${id}-secret`}>{copy.appSecret}
          <input ref={secretRef} id={`${id}-secret`} className={styles.keyInput} type="password" autoComplete="new-password" spellCheck={false}
            required disabled={saving} maxLength={4096} aria-invalid={feedback === 'invalid' || undefined} aria-describedby={`${id}-privacy`}
            onChange={() => setFeedback(null)} />
        </label>
        <Button type="submit" variant="primary" size="sm" loading={saving} disabled={saving}>
          {saving ? copy.preparing : copy.prepare}
        </Button>
      </div>
      {feedback && <p className={feedback === 'ready' ? styles.setupCardBody : styles.errorText} role={feedback === 'ready' ? 'status' : 'alert'}>{copy[feedback]}</p>}
      {feedback === 'ready' && <Link className={styles.adsLink} to="/anuncios">{copy.ads}</Link>}
    </form>
  )
}

// ── Web search (Brave) card ───────────────────────────────────────────────────

interface WebSearchCardProps {
  status: WebSearchStatus
  onSaved: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function WebSearchCard({ status, onSaved, onToast }: WebSearchCardProps) {
  const t = useT()
  const [saving, setSaving] = useState(false)
  const keyRef = useRef<HTMLInputElement>(null)

  async function handleSave() {
    if (saving) return
    const key = keyRef.current?.value.trim() ?? ''
    if (!key) { onToast(t('int.brave.err.enter_key'), 'warn'); return }
    setSaving(true)
    try {
      const r = await setWebSearchKey('brave', key)
      if (r?.ok === false) throw new ApiError(r.error ?? 'error', 0, r)
      if (keyRef.current) keyRef.current.value = ''
      onSaved()
    } catch (e) {
      onToast(t('int.brave.err.activate').replace('{reason}', e instanceof Error ? e.message : t('int.err.generic')), 'error')
    } finally { setSaving(false) }
  }

  return (
    <div className={styles.setupCard}>
      <p className={styles.setupCardTitle}>{t('int.brave.setup.title')}</p>
      <p className={styles.setupCardBody}>
        {t('int.brave.setup.body')}
      </p>
      <details className={styles.setupCardSteps}><summary>{t('int.setup.help')}</summary><p>
        {t('int.composio.setup.step1')}{' '}
        <a href="https://api.search.brave.com/app/keys" target="_blank" rel="noopener noreferrer">
          api.search.brave.com
        </a>
        {'  ·  '}{t('int.brave.setup.step2')}{'  ·  '}
        {t('int.brave.setup.step3')}
      </p></details>

      <div
        className={[styles.wsStatus, status.brave ? styles.wsStatusActive : ''].filter(Boolean).join(' ')}
        aria-live="polite"
      >
        {status.brave && <Check size={12} aria-hidden="true" />}
        <span>
          {status.brave
            ? t('int.brave.status.active')
            : t(status.ddgs_fallback ? 'int.brave.status.fallback' : 'int.brave.status.unconfigured')}
        </span>
      </div>

      <div className={styles.formInline}>
        <label className="sr-only" htmlFor="brave-key">{t('int.brave.key.label')}</label>
        <input
          id="brave-key"
          ref={keyRef}
          className={styles.keyInput}
          type="password"
          placeholder={t('int.brave.key.label')}
          autoComplete="new-password"
          onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) void handleSave() }}
        />
        <Button
          variant="primary"
          size="sm"
          onClick={handleSave}
          disabled={saving}
          loading={saving}
        >
          {saving ? t('int.brave.activating') : t('int.brave.activate_btn')}
        </Button>
      </div>
    </div>
  )
}
