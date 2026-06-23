import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import {
  listProviders, listNativeProviders, addProvider, setActiveProvider,
  testProvider, deleteProvider, startProviderOAuth, getProviderOAuthStatus,
  ApiError,
} from '../api/client'
import type { Provider } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'

// Mirrors vanilla providers.js: badge colours per kind/auth-type
const KIND_COLORS: Record<string, string> = {
  anthropic: '#D97706', openai: '#10A37F', openai_compatible: '#10A37F',
  google: '#4285F4', gemini: '#4285F4', azure: '#0078D4', mistral: '#FF7000',
  groq: '#F55036', ollama: '#6B7280', nous: '#7C3AED', cohere: '#39594D',
  vllm: '#7C3AED', oauth: '#8B5CF6', 'api key': '#6B7280', subscription: '#8B5CF6',
  modelo: '#6B7280',
}

const OAUTH_IDS = new Set(['nous', 'openai-codex', 'xai-oauth'])

function badgeLabel(p: Provider): string {
  if (p.kind) return p.kind
  const a = String(p.auth_type ?? '').toLowerCase()
  if (a.includes('oauth')) return 'OAuth'
  if (a.includes('api')) return 'API key'
  return 'Modelo'
}

function isOAuthProvider(p: Provider): boolean {
  const id = p.provider_id ?? ''
  return Boolean(p.supports_oauth)
    || /oauth/i.test(String(p.auth_type ?? ''))
    || OAUTH_IDS.has(id)
}

function providerName(p: Provider): string {
  return p.alias ?? p.name ?? p.provider_id ?? ''
}

// Discriminated state to make impossible combinations unreachable
type State =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'success'; configured: Provider[]; native: Provider[] }

type Action =
  | { type: 'LOADED'; configured: Provider[]; native: Provider[] }
  | { type: 'FAILED'; message: string }
  | { type: 'RELOAD' }

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case 'LOADED': return { status: 'success', configured: action.configured, native: action.native }
    case 'FAILED': return { status: 'error', message: action.message }
    case 'RELOAD': return { status: 'loading' }
  }
}

function show(message: string, kind: 'ok' | 'warn' | 'error' = 'ok') {
  if (kind === 'ok') sileo.success({ title: message })
  else if (kind === 'error') sileo.error({ title: message })
  else sileo.warning({ title: message })
}

export default function ProvidersView() {
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  function load() {
    dispatch({ type: 'RELOAD' })
    // Both calls throw on error — we catch at the combined level so the view
    // shows an honest error instead of silently returning empty arrays.
    Promise.all([listProviders(), listNativeProviders()])
      .then(([configured, native]) => {
        dispatch({
          type: 'LOADED',
          configured: Array.isArray(configured) ? configured : [],
          native: Array.isArray(native) ? native : [],
        })
      })
      .catch((err: unknown) => {
        dispatch({
          type: 'FAILED',
          message: err instanceof ApiError ? err.message : 'No se pudieron cargar los proveedores.',
        })
      })
  }

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const configuredIds = state.status === 'success'
    ? new Set(state.configured.map(p => p.provider_id))
    : new Set<string>()

  return (
    <>
      {ConfirmDialogNode}
      <header className="view-header">
        <h1 className="view-title">Proveedores</h1>
        <p className="view-subtitle">Conecta modelos de IA. Activa el que Lumen usará por defecto.</p>
      </header>

      <div className="view-body cv-view-body">
        {state.status === 'loading' && (
          <div className="state-container" aria-busy="true" aria-live="polite">
            <p className="state-label">Cargando proveedores…</p>
          </div>
        )}

        {state.status === 'error' && (
          <div className="state-container" role="alert">
            <p className="state-error">{state.message}</p>
            <button className="cv-btn cv-btn--secondary" onClick={load}>Reintentar</button>
          </div>
        )}

        {state.status === 'success' && (
          <>
            <section className="cv-section" aria-label="Proveedores configurados">
              <h2 className="cv-section-label">Configurados</h2>
              {state.configured.length === 0
                ? <p className="cv-empty">Sin proveedores configurados. Añade uno del catálogo.</p>
                : (
                  <ul className="cv-list" role="list">
                    {state.configured.map(p => (
                      <li key={p.provider_id}>
                        <ProviderRow
                          provider={p}
                          isConfigured
                          onRefresh={load}
                          onToast={show}
                          onConfirm={confirm}
                        />
                      </li>
                    ))}
                  </ul>
                )
              }
            </section>

            <section className="cv-section" aria-label="Modelo propio o local">
              <h2 className="cv-section-label">Modelo propio / local</h2>
              <CustomProviderCard onAdded={load} onToast={show} />
            </section>

            <section className="cv-section" aria-label="Catálogo nativo Hermes">
              <h2 className="cv-section-label">Catálogo nativo Hermes</h2>
              {state.native.length === 0
                ? <p className="cv-empty">Catálogo no disponible en esta versión.</p>
                : (
                  <ul className="cv-list" role="list">
                    {state.native
                      .filter(p => !configuredIds.has(p.provider_id))
                      .map(p => (
                        <li key={p.provider_id}>
                          <ProviderRow
                            provider={p}
                            isConfigured={false}
                            onRefresh={load}
                            onToast={show}
                            onConfirm={confirm}
                          />
                        </li>
                      ))
                    }
                  </ul>
                )
              }
            </section>
          </>
        )}
      </div>
    </>
  )
}

// ── Provider row ──────────────────────────────────────────────────────────────

type ConfirmFn = (opts: import('../components/ConfirmDialog').ConfirmOptions) => Promise<boolean>

interface ProviderRowProps {
  provider: Provider
  isConfigured: boolean
  onRefresh: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
  onConfirm: ConfirmFn
}

function ProviderRow({ provider, isConfigured, onRefresh, onToast, onConfirm }: ProviderRowProps) {
  const [testing, setTesting] = useState(false)
  const [oauthPending, setOauthPending] = useState(false)
  const [showKeyForm, setShowKeyForm] = useState(false)
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [addingKey, setAddingKey] = useState(false)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const label = badgeLabel(provider)
  const color = KIND_COLORS[label.toLowerCase()] ?? '#6B7280'
  const name = providerName(provider)
  const id = provider.provider_id ?? ''

  async function handleActivate() {
    try {
      await setActiveProvider(id)
      onToast(`${name} activado — ya puedes chatear`, 'ok')
      onRefresh()
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    }
  }

  async function handleTest() {
    setTesting(true)
    try {
      const r = await testProvider(id)
      onToast(r?.ok ? 'Conexión exitosa' : 'Sin respuesta del servidor', r?.ok ? 'ok' : 'warn')
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    } finally { setTesting(false) }
  }

  async function handleDelete() {
    const isActive = isConfigured && provider.is_active
    const ok = await onConfirm({
      title: `¿Eliminar ${name}?`,
      description: isActive
        ? 'Este es el proveedor activo. Si lo eliminas, el chat dejará de funcionar hasta que configures otro.'
        : undefined,
      confirmLabel: 'Eliminar',
      variant: 'danger',
    })
    if (!ok) return
    try {
      await deleteProvider(id)
      onToast('Proveedor eliminado', 'ok')
      onRefresh()
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    }
  }

  async function handleAddConfirm() {
    if (!apiKeyInput.trim()) { onToast('Introduce la clave API', 'warn'); return }
    setAddingKey(true)
    try {
      await addProvider({
        provider_id: id,
        alias: provider.alias ?? provider.name,
        api_key: apiKeyInput.trim(),
        kind: provider.kind ?? provider.category,
      })
      // Activate immediately so the user can chat right away
      await setActiveProvider(id)
      setShowKeyForm(false)
      setApiKeyInput('')
      // Test the connection so the user gets clear feedback on key validity
      try {
        const r = await testProvider(id)
        if (r?.ok) {
          onToast(`${name} conectado y verificado — pruébalo en el chat`, 'ok')
        } else {
          onToast(`${name} añadido, pero la conexión falló. Revisa la clave API.`, 'warn')
        }
      } catch {
        onToast(`${name} añadido, pero la conexión falló. Revisa la clave API.`, 'warn')
      }
      onRefresh()
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    } finally {
      setAddingKey(false)
    }
  }

  async function handleOAuth() {
    setOauthPending(true)
    let r: Record<string, unknown>
    try {
      r = await startProviderOAuth(id)
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'No se pudo conectar', 'error')
      setOauthPending(false)
      return
    }

    if (!r || r['error']) {
      onToast(`No se pudo conectar: ${(r?.['error'] as string) ?? 'error desconocido'}`, 'error')
      setOauthPending(false)
      return
    }

    const session = r['session_id'] as string | undefined
    const url = (r['auth_url'] ?? r['verification_url']) as string | undefined
    const code = r['user_code'] as string | undefined

    if (url) {
      window.open(url, '_blank', 'noopener,noreferrer')
      onToast(`Abriendo el navegador para conectar ${name}…`, 'ok')
    }
    if (code) {
      onToast(`Ve a ${url ?? ''} e introduce el código: ${code}`, 'ok')
    } else {
      onToast(`Esperando autorización de ${name}…`, 'ok')
    }

    if (!session) { setOauthPending(false); return }

    const intervalMs = Math.max(2000, ((r['poll_interval'] as number | undefined) ?? 4) * 1000)
    const deadline = Date.now() + Math.max(60, ((r['expires_in'] as number | undefined) ?? 600)) * 1000

    const poll = async () => {
      if (Date.now() > deadline) {
        onToast('La sesión de conexión expiró — vuelve a intentarlo', 'warn')
        setOauthPending(false)
        return
      }
      const st = await getProviderOAuthStatus(session)
      const status = String(st?.status ?? '').toLowerCase()
      if (status === 'approved' || status === 'connected' || status === 'success') {
        onToast(`${name} conectado — pruébalo en el chat`, 'ok')
        setOauthPending(false)
        onRefresh()
        return
      }
      if (status === 'error' || status === 'failed') {
        onToast(`No se pudo conectar: ${st?.error_message ?? st?.error ?? 'error desconocido'}`, 'error')
        setOauthPending(false)
        return
      }
      if (status === 'expired') {
        onToast('La sesión de conexión expiró — vuelve a intentarlo', 'warn')
        setOauthPending(false)
        return
      }
      pollRef.current = setTimeout(poll, intervalMs)
    }
    pollRef.current = setTimeout(poll, intervalMs)
  }

  // Cleanup on unmount
  useEffect(() => () => {
    if (pollRef.current) clearTimeout(pollRef.current)
  }, [])

  return (
    <div className={`provider-row${isConfigured && provider.is_active ? ' provider-row--active' : ''}`}>
      <div className="provider-row__left">
        <div className="provider-row__name">{name}</div>
        <div className="provider-row__meta">
          <span
            className="provider-badge"
            style={{ background: `${color}22`, color }}
          >
            {label}
          </span>
          {provider.default_model && (
            <span className="provider-row__model">{provider.default_model}</span>
          )}
          {isConfigured && provider.is_active && (
            <span className="provider-row__active-tag">Activo</span>
          )}
        </div>
      </div>
      <div className="provider-row__actions">
        {isConfigured ? (
          <>
            {!provider.is_active && (
              <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={handleActivate}>
                Activar
              </button>
            )}
            <button
              className="cv-btn cv-btn--ghost cv-btn--sm"
              onClick={handleTest}
              disabled={testing}
            >
              {testing ? 'Probando…' : 'Probar'}
            </button>
            <button
              className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
              onClick={handleDelete}
              aria-label={`Eliminar proveedor ${name}`}
            >
              ✕
            </button>
          </>
        ) : isOAuthProvider(provider) ? (
          <button
            className="cv-btn cv-btn--secondary cv-btn--sm"
            onClick={handleOAuth}
            disabled={oauthPending}
          >
            {oauthPending ? 'Conectando…' : 'Conectar'}
          </button>
        ) : !showKeyForm ? (
          <button
            className="cv-btn cv-btn--secondary cv-btn--sm"
            onClick={() => setShowKeyForm(true)}
          >
            Añadir
          </button>
        ) : null}
      </div>

      {/* Inline masked-input for API key — avoids window.prompt leaking the secret */}
      {!isConfigured && !isOAuthProvider(provider) && showKeyForm && (
        <div className="cv-form-inline" style={{ marginTop: 'var(--sp-3)', flexWrap: 'wrap', gap: 'var(--sp-2)' }}>
          <label className="sr-only" htmlFor={`pv-key-${id}`}>Clave API para {name}</label>
          <input
            id={`pv-key-${id}`}
            className="cv-input"
            type="password"
            autoComplete="new-password"
            placeholder={`Clave API para ${name}`}
            value={apiKeyInput}
            onChange={e => setApiKeyInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') void handleAddConfirm() }}
            style={{ flex: 1, minWidth: 180 }}
          />
          <button
            className="cv-btn cv-btn--primary cv-btn--sm"
            onClick={handleAddConfirm}
            disabled={addingKey}
            type="button"
          >
            {addingKey ? 'Guardando…' : 'Guardar'}
          </button>
          <button
            className="cv-btn cv-btn--ghost cv-btn--sm"
            onClick={() => { setShowKeyForm(false); setApiKeyInput('') }}
            type="button"
          >
            Cancelar
          </button>
        </div>
      )}
    </div>
  )
}

// ── Custom provider card (OpenAI-compatible) ──────────────────────────────────

interface CustomProviderCardProps {
  onAdded: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function CustomProviderCard({ onAdded, onToast }: CustomProviderCardProps) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const aliasRef = useRef<HTMLInputElement>(null)
  const urlRef = useRef<HTMLInputElement>(null)
  const modelRef = useRef<HTMLInputElement>(null)
  const keyRef = useRef<HTMLInputElement>(null)

  async function handleSave() {
    const base_url = urlRef.current?.value.trim() ?? ''
    const default_model = modelRef.current?.value.trim() ?? ''
    const alias = aliasRef.current?.value.trim() || default_model || 'Modelo local'
    const api_key = keyRef.current?.value.trim() || undefined

    if (!base_url || !default_model) {
      onToast('Pon al menos la URL base y el nombre del modelo.', 'warn')
      return
    }

    setSaving(true)
    try {
      await addProvider({ kind: 'openai_compatible', alias, default_model, base_url, api_key, set_active: true })
      onToast('Modelo propio añadido y activado — pruébalo en el chat', 'ok')
      setOpen(false)
      if (aliasRef.current) aliasRef.current.value = ''
      if (urlRef.current) urlRef.current.value = ''
      if (modelRef.current) modelRef.current.value = ''
      if (keyRef.current) keyRef.current.value = ''
      onAdded()
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    } finally { setSaving(false) }
  }

  return (
    <div className="cv-teach-card">
      <p className="cv-teach-intro">
        Conecta cualquier servidor compatible: vLLM, LM Studio, Ollama o uno propio.
      </p>
      {!open ? (
        <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={() => setOpen(true)}>
          + Añadir modelo propio
        </button>
      ) : (
        <div className="cv-form-stack">
          <label className="cv-label" htmlFor="pv-c-alias">Nombre</label>
          <input
            id="pv-c-alias"
            ref={aliasRef}
            className="cv-input"
            type="text"
            placeholder='Nombre (p. ej. "Qwen local")'
            autoComplete="off"
          />
          <label className="cv-label" htmlFor="pv-c-url">URL base del servidor</label>
          <input
            id="pv-c-url"
            ref={urlRef}
            className="cv-input"
            type="text"
            placeholder="URL base (p. ej. https://tu-servidor/v1)"
            autoComplete="off"
          />
          <label className="cv-label" htmlFor="pv-c-model">Modelo</label>
          <input
            id="pv-c-model"
            ref={modelRef}
            className="cv-input"
            type="text"
            placeholder="Modelo (p. ej. qwen3.6-35b-a3b)"
            autoComplete="off"
          />
          <label className="cv-label" htmlFor="pv-c-key">Clave API</label>
          {/* Never echo back: password input for secrets */}
          <input
            id="pv-c-key"
            ref={keyRef}
            className="cv-input"
            type="password"
            placeholder="Clave API (si tu servidor la requiere)"
            autoComplete="new-password"
          />
          <p className="cv-hint">La URL base debe terminar en /v1. La clave API solo si tu servidor la pide.</p>
          <div className="cv-form-actions">
            <button
              className="cv-btn cv-btn--primary cv-btn--sm"
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? 'Guardando…' : 'Guardar y activar'}
            </button>
            <button
              className="cv-btn cv-btn--ghost cv-btn--sm"
              onClick={() => setOpen(false)}
            >
              Cancelar
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
