import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { Check, Plug, Globe } from 'lucide-react'
import { useT } from '../lib/i18n'
import {
  getComposioStatus, listComposioConnected, listComposioApps,
  connectComposioApp, setComposioApiKey,
  getWebSearchStatus, setWebSearchKey,
  ApiError,
} from '../api/client'
import type { ComposioStatus, ComposioApp, WebSearchStatus } from '../api/types'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import {
  AnimatePresence,
  AnimatedListItem,
  FadeIn,
  Stagger,
  StaggerItem,
} from '../components/ui/motion'
import styles from './IntegrationsView.module.css'

// Mirrors vanilla integrations.js load order: status first → prevents calling
// connected/apps when Composio has no key (avoids hanging for minutes).

type ComposioState =
  | { status: 'loading' }
  | { status: 'no-key' }
  | { status: 'error'; message: string }
  | { status: 'ready'; info: ComposioStatus; connected: ComposioApp[]; apps: ComposioApp[] }

type ComposioAction =
  | { type: 'LOADING' }
  | { type: 'NO_KEY' }
  | { type: 'FAILED'; message: string }
  | { type: 'READY'; info: ComposioStatus; connected: ComposioApp[]; apps: ComposioApp[] }

function composioReducer(_s: ComposioState, a: ComposioAction): ComposioState {
  switch (a.type) {
    case 'LOADING': return { status: 'loading' }
    case 'NO_KEY': return { status: 'no-key' }
    case 'FAILED': return { status: 'error', message: a.message }
    case 'READY': return { status: 'ready', info: a.info, connected: a.connected, apps: a.apps }
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
  return (
    <div className={styles.skeletonGrid} aria-busy="true" aria-label="Cargando apps">
      {Array.from({ length: 6 }, (_, i) => (
        <div
          key={i}
          className={`skeleton skeleton--card ${styles.skeletonCard}`}
          style={{ animationDelay: `${i * 60}ms` }}
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

  // Clear the reload timer on unmount so it never fires on a dead component
  useEffect(() => {
    return () => {
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
    dispatch({ type: 'LOADING' })
    let status: ComposioStatus
    try {
      status = await getComposioStatus()
    } catch (e) {
      dispatch({
        type: 'FAILED',
        message: e instanceof ApiError ? e.message : 'No se pudo contactar con el servidor de integraciones.',
      })
      return
    }

    if (!status.has_key) {
      dispatch({ type: 'NO_KEY' })
      return
    }

    // Connected/apps errors are surfaced individually to avoid blocking the status row.
    // Returning [] on these is safe — we tell the user which part failed.
    const [connected, apps] = await Promise.allSettled([
      listComposioConnected(),
      listComposioApps(),
    ])
    dispatch({
      type: 'READY',
      info: status,
      connected: connected.status === 'fulfilled' ? connected.value : [],
      apps: apps.status === 'fulfilled' ? apps.value : [],
    })
  }

  async function loadWebSearch() {
    setWsState({ status: 'loading' })
    try {
      const st = await getWebSearchStatus()
      setWsState({ status: 'ready', data: st })
    } catch (e) {
      setWsState({
        status: 'error',
        message: e instanceof ApiError ? e.message : 'No se pudo cargar el estado de búsqueda web.',
      })
    }
  }

  useEffect(() => {
    loadComposio()
    loadWebSearch()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const connectedSlugs = composioState.status === 'ready'
    ? new Set(composioState.connected.map(c => c.slug))
    : new Set<string>()

  return (
    <>
      <PageHeader
        title="Integraciones"
        subtitle={t('int.subtitle')}
      />

      <div className={`view-body ${styles.body}`}>
        <Stagger style={{ display: 'contents' }}>

          {/* ── Web search (Brave) ─────────────────────────────────────────── */}
          <StaggerItem>
            <section className={styles.section} aria-label="Búsqueda web">
              <h2 className={styles.sectionLabel}>Búsqueda web</h2>

              {wsState.status === 'loading' && (
                <div
                  className="skeleton skeleton--block"
                  style={{ height: 56, borderRadius: 'var(--radius-md)' }}
                  aria-busy="true"
                  aria-label="Cargando estado de búsqueda web"
                />
              )}

              {wsState.status === 'error' && (
                <FadeIn>
                  <div role="alert" className={styles.errorRow}>
                    <p className={styles.errorText}>{wsState.message}</p>
                    <Button variant="secondary" size="sm" onClick={loadWebSearch}>
                      Reintentar
                    </Button>
                  </div>
                </FadeIn>
              )}

              {wsState.status === 'ready' && (
                <WebSearchCard
                  status={wsState.data}
                  onSaved={() => { loadWebSearch(); show('Brave activado — las búsquedas del agente ya usan Brave', 'ok') }}
                  onToast={show}
                />
              )}
            </section>
          </StaggerItem>

          {/* ── Composio connection status ────────────────────────────────── */}
          <StaggerItem>
            <section className={styles.section} aria-label="Servicios conectados">
              <h2 className={styles.sectionLabel}>Conecta tus apps</h2>

              {composioState.status === 'loading' && (
                <div
                  className="skeleton skeleton--block"
                  style={{ height: 48, borderRadius: 'var(--radius-md)' }}
                  aria-busy="true"
                  aria-label="Comprobando conexión con Composio"
                />
              )}

              {composioState.status === 'no-key' && (
                <ComposioSetupCard
                  onSaved={() => {
                    loadComposio()
                    show('Composio conectado — ahora puedes conectar tus apps', 'ok')
                  }}
                  onToast={show}
                />
              )}

              {composioState.status === 'error' && (
                <FadeIn>
                  <div role="alert" className={styles.errorRow}>
                    <p className={styles.errorText}>{composioState.message}</p>
                    <Button variant="secondary" size="sm" onClick={loadComposio}>
                      Reintentar
                    </Button>
                  </div>
                </FadeIn>
              )}

              {composioState.status === 'ready' && (
                <FadeIn>
                  <div className={styles.statusBanner} aria-label="Composio activo">
                    <Check
                      size={14}
                      className={styles.statusBannerCheck}
                      aria-hidden="true"
                    />
                    <span>
                      Composio activo · Cuenta:{' '}
                      <code className={styles.statusBannerCode}>
                        {composioState.info.entity_id ?? '—'}
                      </code>
                    </span>
                  </div>
                </FadeIn>
              )}
            </section>
          </StaggerItem>

          {/* ── Connected apps ────────────────────────────────────────────── */}
          <StaggerItem>
            <section className={styles.section} aria-label="Apps conectadas">
              <h2 className={styles.sectionLabel}>Conectadas</h2>

              {composioState.status === 'loading' && <AppGridSkeleton />}

              {(composioState.status === 'no-key' || composioState.status === 'error') && (
                <p className={styles.lockedPlaceholder}>
                  Conecta Composio (arriba) para ver y gestionar tus apps.
                </p>
              )}

              {composioState.status === 'ready' && (
                composioState.connected.length === 0
                  ? (
                    <EmptyState
                      compact
                      icon={<Plug size={28} />}
                      title="Sin apps conectadas todavía"
                      description="Conéctate a Gmail, Slack, Notion y más desde el catálogo de abajo."
                    />
                  )
                  : (
                    <ul className={styles.appGrid} role="list">
                      <AnimatePresence initial={false}>
                        {composioState.connected.map(app => (
                          <AnimatedListItem key={app.slug}>
                            <AppCard app={app} isConnected />
                          </AnimatedListItem>
                        ))}
                      </AnimatePresence>
                    </ul>
                  )
              )}
            </section>
          </StaggerItem>

          {/* ── Available apps ────────────────────────────────────────────── */}
          <StaggerItem>
            <section className={styles.section} aria-label="Apps disponibles">
              <h2 className={styles.sectionLabel}>Catálogo</h2>

              {composioState.status === 'loading' && <AppGridSkeleton />}

              {(composioState.status === 'no-key' || composioState.status === 'error') && (
                <p className={styles.lockedPlaceholder}>
                  Conecta Composio (arriba) para explorar el catálogo de apps.
                </p>
              )}

              {composioState.status === 'ready' && (() => {
                const remaining = composioState.apps.filter(a => !connectedSlugs.has(a.slug))
                return remaining.length === 0
                  ? (
                    <EmptyState
                      icon={<Globe size={28} />}
                      title="Todo conectado"
                      description="No hay más apps disponibles en tu cuenta de Composio."
                    />
                  )
                  : (
                    <ul className={styles.appGrid} role="list">
                      <AnimatePresence initial={false}>
                        {remaining.map(app => (
                          <AnimatedListItem key={app.slug}>
                            <AppCard
                              app={app}
                              isConnected={false}
                              onConnect={async (a) => {
                                try {
                                  const r = await connectComposioApp(a.slug)
                                  if (r?.redirect_url) {
                                    window.open(r.redirect_url, '_blank', 'noopener,noreferrer')
                                  }
                                  show(`Conectando ${a.name ?? a.slug}… completa la autorización en el navegador`, 'info')
                                  reloadTimerRef.current = setTimeout(loadComposio, 3000)
                                } catch (e) {
                                  show(e instanceof Error ? e.message : 'Error', 'error')
                                }
                              }}
                            />
                          </AnimatedListItem>
                        ))}
                      </AnimatePresence>
                    </ul>
                  )
              })()}
            </section>
          </StaggerItem>

        </Stagger>
      </div>
    </>
  )
}

// ── App card (grid item) ──────────────────────────────────────────────────────

interface AppCardProps {
  app: ComposioApp
  isConnected: boolean
  onConnect?: (app: ComposioApp) => void
}

function AppCard({ app, isConnected, onConnect }: AppCardProps) {
  const displayName =
    app.name ??
    (app as unknown as Record<string, unknown>).toolkit_slug as string | undefined ??
    app.slug ??
    '—'

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
        {app.description && (
          <div className={styles.appDesc} title={app.description}>
            {app.description}
          </div>
        )}
      </div>

      <div className={styles.appAction}>
        {isConnected
          ? (
            <span className={styles.connectedBadge}>
              <Check size={10} aria-hidden="true" />
              Conectado
            </span>
          )
          : (
            <button
              className={styles.connectBtn}
              aria-label={`Conectar ${displayName}`}
              onClick={() => onConnect?.(app)}
            >
              Conectar
            </button>
          )
        }
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
  const [saving, setSaving] = useState(false)
  const keyRef = useRef<HTMLInputElement>(null)

  async function handleSave() {
    const key = keyRef.current?.value.trim() ?? ''
    if (!key) { onToast('Introduce una clave API', 'warn'); return }
    setSaving(true)
    try {
      await setComposioApiKey(key)
      if (keyRef.current) keyRef.current.value = ''
      onSaved()
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    } finally { setSaving(false) }
  }

  return (
    <div className={styles.setupCard}>
      <p className={styles.setupCardTitle}>Conecta tus apps del día a día</p>
      <p className={styles.setupCardBody}>
        Gmail, Slack, Notion y más de 250 servicios. Gratis para empezar.
      </p>
      <p className={styles.setupCardSteps}>
        1) Entra en{' '}
        <a href="https://app.composio.dev/developers" target="_blank" rel="noopener noreferrer">
          app.composio.dev
        </a>
        {'  ·  '}2) Crea una cuenta gratis{'  ·  '}3) En{' '}
        <strong>Settings → API Keys</strong> genera una clave y pégala aquí.
      </p>
      <div className={styles.formInline}>
        <label className="sr-only" htmlFor="composio-apikey">Clave de acceso</label>
        {/* Secret: password input, never echoed back */}
        <input
          id="composio-apikey"
          ref={keyRef}
          className={styles.keyInput}
          type="password"
          placeholder="Pega tu clave de acceso"
          autoComplete="new-password"
          onKeyDown={e => { if (e.key === 'Enter') handleSave() }}
        />
        <Button
          variant="primary"
          size="sm"
          onClick={handleSave}
          disabled={saving}
          loading={saving}
        >
          {saving ? 'Conectando…' : 'Conectar'}
        </Button>
      </div>
    </div>
  )
}

// ── Web search (Brave) card ───────────────────────────────────────────────────

interface WebSearchCardProps {
  status: WebSearchStatus
  onSaved: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function WebSearchCard({ status, onSaved, onToast }: WebSearchCardProps) {
  const [saving, setSaving] = useState(false)
  const keyRef = useRef<HTMLInputElement>(null)

  async function handleSave() {
    const key = keyRef.current?.value.trim() ?? ''
    if (!key) { onToast('Pega tu clave API de Brave', 'warn'); return }
    setSaving(true)
    try {
      const r = await setWebSearchKey('brave', key)
      if (r?.ok === false) throw new ApiError(r.error ?? 'error', 0, r)
      if (keyRef.current) keyRef.current.value = ''
      onSaved()
    } catch (e) {
      onToast(`No se pudo activar: ${e instanceof Error ? e.message : 'error'}`, 'error')
    } finally { setSaving(false) }
  }

  return (
    <div className={styles.setupCard}>
      <p className={styles.setupCardTitle}>Mejora tus búsquedas con Brave</p>
      <p className={styles.setupCardBody}>
        Lumen ya busca en la web (DuckDuckGo, sin configurar). Con Brave obtienes
        resultados más precisos y sin rastreo.
      </p>
      <p className={styles.setupCardSteps}>
        1) Entra en{' '}
        <a href="https://api.search.brave.com/app/keys" target="_blank" rel="noopener noreferrer">
          api.search.brave.com
        </a>
        {'  ·  '}2) Crea una cuenta y elige el plan gratuito (Free){'  ·  '}
        3) Genera una clave API y pégala aquí.
      </p>

      <div
        className={[styles.wsStatus, status.brave ? styles.wsStatusActive : ''].filter(Boolean).join(' ')}
        aria-live="polite"
      >
        {status.brave && <Check size={12} aria-hidden="true" />}
        <span>
          {status.brave
            ? 'Brave activo · DuckDuckGo de reserva'
            : 'Activo: DuckDuckGo (sin clave Brave)'}
        </span>
      </div>

      <div className={styles.formInline}>
        <label className="sr-only" htmlFor="brave-key">Clave API de Brave Search</label>
        <input
          id="brave-key"
          ref={keyRef}
          className={styles.keyInput}
          type="password"
          placeholder="Clave API de Brave Search"
          autoComplete="new-password"
          onKeyDown={e => { if (e.key === 'Enter') handleSave() }}
        />
        <Button
          variant="primary"
          size="sm"
          onClick={handleSave}
          disabled={saving}
          loading={saving}
        >
          {saving ? 'Activando…' : 'Activar Brave'}
        </Button>
      </div>
    </div>
  )
}
