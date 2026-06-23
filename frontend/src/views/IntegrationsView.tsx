import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import {
  getComposioStatus, listComposioConnected, listComposioApps,
  connectComposioApp, setComposioApiKey,
  getWebSearchStatus, setWebSearchKey,
  ApiError,
} from '../api/client'
import type { ComposioStatus, ComposioApp, WebSearchStatus } from '../api/types'

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

export default function IntegrationsView() {
  const [composioState, dispatch] = useReducer(composioReducer, { status: 'loading' })
  const [wsState, setWsState] = useState<WsState>({ status: 'loading' })
  const reloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Clear the reload timer on unmount so it never fires on a dead component
  useEffect(() => {
    return () => {
      if (reloadTimerRef.current !== null) clearTimeout(reloadTimerRef.current)
    }
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
      <header className="view-header">
        <h1 className="view-title">Integraciones</h1>
        <p className="view-subtitle">Conecta Lumen a tus apps. Más de 250 conectores disponibles vía Composio.</p>
      </header>

      <div className="view-body cv-view-body">
        {/* ── Web search (Brave) ─────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Búsqueda web">
          <h2 className="cv-section-label">Búsqueda web</h2>
          {wsState.status === 'loading' && <div className="cv-skeleton" aria-busy="true" />}
          {wsState.status === 'error' && (
            <div role="alert">
              <p className="state-error">{wsState.message}</p>
              <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={loadWebSearch} style={{ marginTop: 8 }}>
                Reintentar
              </button>
            </div>
          )}
          {wsState.status === 'ready' && (
            <WebSearchCard
              status={wsState.data}
              onSaved={() => { loadWebSearch(); show('Brave activado — las búsquedas del agente ya usan Brave', 'ok') }}
              onToast={show}
            />
          )}
        </section>

        {/* ── Composio status ────────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Estado Composio">
          <h2 className="cv-section-label">Composio — conecta tus apps</h2>
          {composioState.status === 'loading' && (
            <div className="cv-skeleton" aria-busy="true" />
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
            <div role="alert">
              <p className="state-error">{composioState.message}</p>
              <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={loadComposio} style={{ marginTop: 8 }}>
                Reintentar
              </button>
            </div>
          )}
          {composioState.status === 'ready' && (
            <div className="integration-status-ok" aria-label="Composio activo">
              <span className="integration-status-ok__check" aria-hidden="true">✓</span>
              Composio activo · Tu cuenta: <code>{composioState.info.entity_id ?? ''}</code>
            </div>
          )}
        </section>

        {/* ── Connected apps ─────────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Apps conectadas">
          <h2 className="cv-section-label">Conectadas</h2>
          {composioState.status === 'loading' && <div className="cv-skeleton" aria-busy="true" />}
          {(composioState.status === 'no-key' || composioState.status === 'error') && (
            <p className="cv-empty">Conecta Composio (arriba) para ver y conectar tus apps.</p>
          )}
          {composioState.status === 'ready' && (
            composioState.connected.length === 0
              ? <p className="cv-empty">Sin apps conectadas.</p>
              : (
                <ul className="cv-list" role="list">
                  {composioState.connected.map(app => (
                    <li key={app.slug}>
                      <AppRow app={app} isConnected />
                    </li>
                  ))}
                </ul>
              )
          )}
        </section>

        {/* ── Available apps ──────────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Apps disponibles">
          <h2 className="cv-section-label">Apps disponibles</h2>
          {composioState.status === 'loading' && <div className="cv-skeleton" aria-busy="true" />}
          {(composioState.status === 'no-key' || composioState.status === 'error') && (
            <p className="cv-empty">Conecta Composio (arriba) para ver y conectar tus apps.</p>
          )}
          {composioState.status === 'ready' && (() => {
            const remaining = composioState.apps.filter(a => !connectedSlugs.has(a.slug))
            return remaining.length === 0
              ? <p className="cv-empty">Sin apps adicionales disponibles.</p>
              : (
                <ul className="cv-list" role="list">
                  {remaining.map(app => (
                    <li key={app.slug}>
                      <AppRow
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

// ── App row ───────────────────────────────────────────────────────────────────

interface AppRowProps {
  app: ComposioApp
  isConnected: boolean
  onConnect?: (app: ComposioApp) => void
}

function AppRow({ app, isConnected, onConnect }: AppRowProps) {
  return (
    <div className={`integration-row${isConnected ? ' integration-row--connected' : ''}`}>
      <div className="integration-row__icon" aria-hidden="true">
        {app.logo
          ? <img src={app.logo} alt="" width={20} height={20} />
          : <span className="integration-row__icon-fallback">⊞</span>
        }
      </div>
      <div className="integration-row__info">
        <div className="integration-row__name">{app.name ?? app.slug}</div>
        {app.description && (
          <div className="integration-row__desc">{app.description}</div>
        )}
      </div>
      <div className="integration-row__status">
        {isConnected
          ? <span className="integration-connected-tag">✓ Conectado</span>
          : (
            <button
              className="cv-btn cv-btn--secondary cv-btn--sm"
              aria-label={`Conectar ${app.name ?? app.slug}`}
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
    <div className="cv-teach-card">
      <p className="cv-teach-intro">
        Composio conecta Lumen a tus apps del día a día (Gmail, Slack, Notion y más de 250). Es gratis para empezar.
      </p>
      <p className="cv-teach-intro">
        1) Entra en{' '}
        <a href="https://app.composio.dev/developers" target="_blank" rel="noopener noreferrer">app.composio.dev</a>
        {' '}· 2) Crea una cuenta gratis · 3) En <strong>Settings → API Keys</strong> genera una clave (<code>ak_…</code>) y pégala aquí.
      </p>
      <div className="cv-form-inline">
        <label className="sr-only" htmlFor="composio-apikey">Clave API de Composio</label>
        {/* Secret: password input, never echoed back */}
        <input
          id="composio-apikey"
          ref={keyRef}
          className="cv-input"
          type="password"
          placeholder="Clave API de Composio"
          autoComplete="new-password"
          onKeyDown={e => { if (e.key === 'Enter') handleSave() }}
        />
        <button
          className="cv-btn cv-btn--primary cv-btn--sm"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? 'Conectando…' : 'Conectar'}
        </button>
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
    <div className="cv-teach-card">
      <div className="cv-teach-card__title-row">
        <strong>Mejora tus búsquedas web con Brave</strong>
      </div>
      <p className="cv-teach-intro">
        Lumen ya busca en la web (DuckDuckGo, sin configurar). Para resultados más fiables y de mayor
        calidad, añade una clave API gratuita de Brave Search.
      </p>
      <p className="cv-hint">
        1) Entra en{' '}
        <a href="https://api.search.brave.com/app/keys" target="_blank" rel="noopener noreferrer">
          api.search.brave.com
        </a>
        {'  ·  '}2) Crea una cuenta y elige el plan gratuito (Free){'  ·  '}
        3) Genera una clave API y pégala aquí.
      </p>
      <div className={`websearch-status${status.brave ? ' is-active' : ''}`} aria-live="polite">
        {status.brave
          ? '✓ Brave activo · DuckDuckGo de reserva'
          : 'Activo: DuckDuckGo (sin clave)'}
      </div>
      <div className="cv-form-inline">
        <label className="sr-only" htmlFor="brave-key">Clave API de Brave Search</label>
        <input
          id="brave-key"
          ref={keyRef}
          className="cv-input"
          type="password"
          placeholder="Clave API de Brave Search"
          autoComplete="new-password"
          onKeyDown={e => { if (e.key === 'Enter') handleSave() }}
        />
        <button
          className="cv-btn cv-btn--primary cv-btn--sm"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? 'Activando…' : 'Activar Brave'}
        </button>
      </div>
    </div>
  )
}
