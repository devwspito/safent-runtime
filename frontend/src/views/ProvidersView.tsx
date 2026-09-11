import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { AlertCircle, Cloud, Cpu, Globe, Server } from 'lucide-react'
import { useT } from '../lib/i18n'
import {
  listProviders, listNativeProviders, getNativeActive, addProvider, configureNativeProvider, setActiveProvider,
  testProvider, deleteProvider, startProviderOAuth, getProviderOAuthStatus,
  ApiError,
} from '../api/client'
import type { Provider } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'
import Badge from '../components/Badge'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import {
  AnimatePresence,
  AnimatedListItem,
  AnimatedExpanderContent,
  motion,
  useReducedMotion,
  SPRING,
  TWEEN_FAST,
} from '../components/ui/motion'
import css from './ProvidersView.module.css'

// ── Kind colours — semantic, not decorative ───────────────────────────────────
// Each colour maps to its named brand/palette value; never a pure blue/accent.

const KIND_COLORS: Record<string, string> = {
  anthropic:         '#D97706',
  openai:            '#10A37F',
  openai_compatible: '#10A37F',
  openai_codex:      '#10A37F',
  google:            '#4285F4',
  gemini:            '#4285F4',
  azure:             '#0078D4',
  mistral:           '#FF7000',
  groq:              '#F55036',
  ollama:            '#6B7280',
  nous:              '#7C3AED',
  cohere:            '#39594D',
  vllm:              '#7C3AED',
  oauth:             '#8B5CF6',
  'api key':         '#6B7280',
  subscription:      '#8B5CF6',
  modelo:            '#6B7280',
}

const OAUTH_IDS = new Set(['nous', 'openai-codex', 'xai-oauth'])

// ── OpenAI Codex / ChatGPT (suscripción) — item 4, plan.md D-A4 ────────────────
// Two independent auth paths share this one kind: device-code OAuth (native
// catalogue row, provider_id "openai-codex") and an OPENAI_API_KEY fallback
// (a Provider row of this kind added through the generic add_provider flow —
// see native_sync.kind_to_native_target, which keeps the env_var non-empty
// for CODEX unlike NOUS).
const CODEX_KIND = 'openai_codex'
const CODEX_PROVIDER_ID = 'openai-codex'
// Mirrors hermes.providers.domain.catalog's canonical entry for ProviderKind.CODEX
// (label, default model, alternatives) — a small closed catalog, hardcoded here
// the same way mcpCatalog() curates the MCP suggestions client-side.
const CODEX_MODELS = ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'] as const
const CODEX_DEFAULT_MODEL: string = CODEX_MODELS[0]

function badgeLabel(p: Provider): string {
  if (p.kind) return p.kind
  const a = String(p.auth_type ?? '').toLowerCase()
  if (a.includes('oauth')) return 'OAuth'
  if (a.includes('api')) return 'API key'
  return 'Modelo'
}

// Translated display text for the badge — kept separate from `badgeLabel` so the
// KIND_COLORS lookup (keyed on the raw label) is unaffected by locale.
function badgeDisplayLabel(label: string, t: ReturnType<typeof useT>): string {
  if (label === 'API key') return t('providers.badge.apikey')
  if (label === 'Modelo') return t('providers.badge.model')
  if (label === CODEX_KIND) return t('providers.codex.badge')
  return label
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

// POST /api/v1/providers/native returns {ok:false, error:"oauth_required", auth_type}
// when the native registry entry needs OAuth instead of an api_key (e.g. Codex's
// registry auth_type isn't "api_key") — the caller must pivot to the device-code
// flow rather than surface this as a plain failure.
function isOAuthRequiredError(e: unknown): boolean {
  if (!(e instanceof ApiError)) return false
  const body = e.body as Record<string, unknown> | null
  return body?.['error'] === 'oauth_required' || e.message === 'oauth_required'
}

// ── Discriminated state ───────────────────────────────────────────────────────

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

// ── Device-code OAuth connect — shared by any provider row (native catalogue
// or the Codex onboarding card) so the polling state machine lives in ONE place ─

export function useProviderOAuthConnect(onConnected: () => void) {
  const t = useT()
  const [connectingId, setConnectingId] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ text:string; url?:string; code?:string; error?:boolean } | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const generation = useRef(0)
  const pending = useRef(false)
  const connected = useRef(onConnected)
  connected.current = onConnected

  useEffect(() => () => {
    generation.current++
    pending.current = false
    if (pollRef.current) clearTimeout(pollRef.current)
  }, [])

  async function startOAuthConnect(providerId: string, name: string) {
    if (pending.current) return
    pending.current = true
    const request = ++generation.current
    const current = () => request === generation.current
    const finish = (text:string, error = true) => {
      if (!current()) return
      pending.current = false
      setConnectingId(null)
      setNotice({text,error})
    }
    setConnectingId(providerId)
    setNotice({text:t('providers.oauth.waiting').replace('{name}', name)})
    let r: Record<string, unknown>
    try {
      r = await startProviderOAuth(providerId)
    } catch {
      finish(t('providers.oauth.err.connect'))
      return
    }
    if (!current()) return

    if (!r || r['error']) {
      finish(t('providers.oauth.err.connect'))
      return
    }

    const session = typeof r.session_id === 'string' ? r.session_id : ''
    const rawUrl = r.auth_url ?? r.verification_url
    let url:string
    try {
      const parsed = new URL(String(rawUrl ?? ''))
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password) throw new Error('invalid OAuth URL')
      url = parsed.href
    } catch { finish(t('providers.oauth.err.connect')); return }
    if (!session) { finish(t('providers.oauth.err.connect')); return }
    const code = typeof r.user_code === 'string' ? r.user_code : undefined
    setNotice({text:t('providers.oauth.waiting').replace('{name}', name),url,code})
    // The persistent link also works when the browser blocks an asynchronous popup.
    try { window.open(url, '_blank', 'noopener,noreferrer') } catch { /* link remains available */ }
    const seconds = (value:unknown, fallback:number) => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback
    const intervalMs = Math.min(30000, Math.max(2000, seconds(r.poll_interval,4)*1000))
    const deadline = Date.now() + Math.min(1800, seconds(r.expires_in,600))*1000

    const poll = async () => {
      if (!current()) return
      if (Date.now() > deadline) {
        finish(t('providers.oauth.expired'))
        return
      }
      let st: Awaited<ReturnType<typeof getProviderOAuthStatus>>
      try { st = await getProviderOAuthStatus(session) }
      catch { finish(t('providers.oauth.unverified')); return }
      if (!current()) return
      const status = String(st?.status ?? '').toLowerCase()
      if (status === 'approved' || status === 'connected' || status === 'success') {
        show(t('providers.oauth.connected').replace('{name}', name), 'ok')
        finish(t('providers.oauth.connected').replace('{name}', name), false)
        connected.current()
        return
      }
      if (status === 'error' || status === 'failed') {
        finish(t('providers.oauth.err.connect'))
        return
      }
      if (status === 'expired') {
        finish(t('providers.oauth.expired'))
        return
      }
      if (status !== 'pending') { finish(t('providers.oauth.unverified')); return }
      pollRef.current = setTimeout(poll, intervalMs)
    }
    pollRef.current = setTimeout(poll, intervalMs)
  }

  return { connectingId, startOAuthConnect, notice }
}

function OAuthNotice({ notice }: { notice:ReturnType<typeof useProviderOAuthConnect>['notice'] }) {
  const t = useT()
  if (!notice) return null
  return <div className={css.oauthNotice} role={notice.error ? 'alert' : 'status'}>
    <span>{notice.text}</span>
    {notice.code && <code aria-label={t('providers.oauth.code')}>{notice.code}</code>}
    {notice.url && <a href={notice.url} target="_blank" rel="noopener noreferrer">{t('providers.oauth.open')}</a>}
  </div>
}

// ── Provider kind icon ────────────────────────────────────────────────────────

function ProviderTypeChip({ provider }: { provider: Provider }) {
  const kind = String(provider.kind ?? provider.category ?? '').toLowerCase()
  const isLocal = kind === 'ollama' || kind === 'vllm' || kind === 'openai_compatible'
  const isCloud = kind === 'anthropic' || kind === 'openai' || kind === 'google' ||
    kind === 'gemini' || kind === 'azure' || kind === 'mistral' || kind === 'groq'
  const Icon = isLocal ? Server : isCloud ? Cloud : Globe

  return (
    <span className={css.typeChip} aria-hidden="true">
      <Icon size={13} />
    </span>
  )
}

// ── Skeleton — mirrors the final row layout exactly ───────────────────────────

function SkeletonRows({ count }: { count: number }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      {[...Array(count)].map((_, i) => (
        <div key={i}>
          <div
            className={css.skeletonRow}
            role="presentation"
            aria-hidden="true"
          >
            <div className={`skeleton ${css.skeletonIcon}`} />
            <div className={css.skeletonContent}>
              <div className="skeleton skeleton--line" style={{ width: '38%' }} />
              <div className="skeleton skeleton--line-sm" style={{ width: '22%' }} />
            </div>
            <div className={css.skeletonActions}>
              <div className="skeleton skeleton--chip" />
              <div className="skeleton skeleton--chip" />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Main view ─────────────────────────────────────────────────────────────────

export default function ProvidersView() {
  const t = useT()
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })
  const [confirm, ConfirmDialogNode] = useConfirmDialog()
  const loadGeneration = useRef(0)

  function load() {
    const request = ++loadGeneration.current
    dispatch({ type: 'RELOAD' })
    Promise.all([listProviders(), listNativeProviders(), getNativeActive()])
      .then(([configured, native, nativeActive]) => {
        if (request !== loadGeneration.current) return
        if (!Array.isArray(configured) || !Array.isArray(native)) throw new Error('invalid provider catalog')
        const cfg = configured
        // Native-configured providers live in a separate store from the repo;
        // surface the active one in the configured list so a just-added native
        // catalogue provider is actually visible + marked active. But when a
        // *custom* provider is active, the daemon mirrors it into the native
        // store under a generic id (e.g. "openai-api") that points at the SAME
        // base_url — that mirror is not a distinct provider, so skip it (else
        // the one provider renders as two "Activo" cards).
        const sameEndpoint = (u?: string) => (u ?? '').trim().replace(/\/+$/, '')
        const nativeAlreadyShown = !!nativeActive && cfg.some(p =>
          p.provider_id === nativeActive.provider_id ||
          (p.is_active === true &&
            sameEndpoint(p.base_url) !== '' &&
            sameEndpoint(p.base_url) === sameEndpoint(nativeActive.base_url) &&
            (!nativeActive.default_model || !p.default_model ||
              nativeActive.default_model === p.default_model)))
        const merged = nativeActive && !nativeAlreadyShown
          ? [nativeActive, ...cfg]
          : cfg
        // If we collapsed the native mirror (same endpoint as an active custom
        // provider, not a distinct one), also drop it from the catalogue — else it
        // reappears as an "add" option that writes to the SAME shared native slot
        // and clobbers the active provider.
        const nativeList = Array.isArray(native) ? native : []
        const mirrorCollapsed = !!nativeActive && nativeAlreadyShown &&
          !cfg.some(p => p.provider_id === nativeActive.provider_id)
        const filteredNative = mirrorCollapsed
          ? nativeList.filter(n => n.provider_id !== nativeActive!.provider_id)
          : nativeList
        dispatch({
          type: 'LOADED',
          configured: merged,
          native: filteredNative,
        })
      })
      .catch(() => {
        if (request !== loadGeneration.current) return
        dispatch({
          type: 'FAILED',
          message: t('providers.err.load'),
        })
      })
  }

  useEffect(() => { load(); return () => { loadGeneration.current++ } }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const configuredIds = state.status === 'success'
    ? new Set(state.configured.map(p => p.provider_id))
    : new Set<string>()

  const codexAlreadyConfigured = state.status === 'success' && state.configured.some(
    p => p.kind === CODEX_KIND || p.provider_id === CODEX_PROVIDER_ID,
  )

  return (
    <>
      {ConfirmDialogNode}
      <div className={css.header}><PageHeader
        title={t('providers.title')}
        subtitle={t('providers.subtitle')}
        actions={<Button variant="secondary" size="sm" onClick={load} disabled={state.status === 'loading'}>{t('providers.refresh')}</Button>}
      />

      </div><div className={`view-body ${css.body}`}>
        {state.status === 'loading' && (
          <section className={css.section} aria-label={t('providers.loading_aria')}>
            <div className={css.sectionLabel} aria-hidden="true">{t('providers.section.configured')}</div>
            <SkeletonRows count={3} />
          </section>
        )}

        {state.status === 'error' && (
          <div>
            <div className={css.errorBox} role="alert">
              <AlertCircle size={16} style={{ color: 'var(--color-danger)', flexShrink: 0, marginTop: 1 }} aria-hidden="true" />
              <span className={css.errorText}>{state.message}</span>
              <Button variant="secondary" size="sm" onClick={load}>
                {t('providers.retry')}
              </Button>
            </div>
          </div>
        )}

        {state.status === 'success' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>

            {/* ── Configured providers ── */}
            <div>
              <section className={css.section} aria-label={t('providers.section.configured.aria')}>
                <h2 className={css.sectionLabel}>{t('providers.section.configured')}</h2>
                {state.configured.length === 0 ? (
                  <EmptyState
                    compact
                    icon={<Cpu size={32} />}
                    title={t('providers.empty.title')}
                    description={t('providers.empty.desc')}
                    action={
                      <Button variant="primary" size="sm" onClick={() => {
                        document.getElementById('pv-catalogue')?.scrollIntoView({ behavior: 'instant' })
                      }}>
                        {t('providers.empty.cta')}
                      </Button>
                    }
                  />
                ) : (
                  <ul className={css.list} role="list">
                    <AnimatePresence initial={false}>
                      {state.configured.map(p => (
                        <AnimatedListItem key={p.provider_id}>
                          <ProviderRow
                            provider={p}
                            isConfigured
                            onRefresh={load}
                            onToast={show}
                            onConfirm={confirm}
                          />
                        </AnimatedListItem>
                      ))}
                    </AnimatePresence>
                  </ul>
                )}
              </section>
            </div>

            {/* ── Custom / local model ── */}
            <div>
              <section className={css.section} aria-label={t('providers.section.custom.aria')}>
                <h2 className={css.sectionLabel}>{t('providers.section.custom')}</h2>
                <CustomProviderCard onAdded={load} onToast={show} />
              </section>
            </div>

            {/* ── OpenAI Codex / ChatGPT (suscripción) ── */}
            {!codexAlreadyConfigured && (
              <div>
                <section className={css.section} aria-label={t('providers.section.codex.aria')}>
                  <h2 className={css.sectionLabel}>{t('providers.section.codex')}</h2>
                  <CodexProviderCard onAdded={load} onToast={show} />
                </section>
              </div>
            )}

            {/* ── Native Hermes catalogue ── */}
            <div>
              <section
                id="pv-catalogue"
                className={css.section}
                aria-label={t('providers.section.native.aria')}
              >
                <h2 className={css.sectionLabel}>{t('providers.section.native')}</h2>
                {state.native.length === 0 ? (
                  <p className={css.catalogueEmpty}>
                    {t('providers.native.empty')}
                  </p>
                ) : (
                  <ul className={css.list} role="list">
                    <AnimatePresence initial={false}>
                      {state.native
                        // Codex has its own dedicated onboarding card above (model
                        // picker + the two auth paths explained) — don't also list
                        // its bare native-catalogue row here, it would just be a
                        // second, less informative "Connect" button for the SAME
                        // provider_id.
                        .filter(p => !configuredIds.has(p.provider_id) && p.provider_id !== CODEX_PROVIDER_ID)
                        .map(p => (
                          <AnimatedListItem key={p.provider_id}>
                            <ProviderRow
                              provider={p}
                              isConfigured={false}
                              onRefresh={load}
                              onToast={show}
                              onConfirm={confirm}
                            />
                          </AnimatedListItem>
                        ))
                      }
                    </AnimatePresence>
                  </ul>
                )}
              </section>
            </div>

          </div>
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

export function ProviderRow({ provider, isConfigured, onRefresh, onToast, onConfirm }: ProviderRowProps) {
  const t = useT()
  const [testing, setTesting] = useState(false)
  const [showKeyForm, setShowKeyForm] = useState(false)
  const [apiKeyInput, setApiKeyInput] = useState('')
  // Pre-filled from the native catalogue's suggested default_model (server-side
  // curated table) when known; always editable — the owner can type any model
  // the provider serves. PROV-02: without a model, configureNativeProvider()
  // saved config.yaml with no model.default and the first chat crashed.
  const [modelInput, setModelInput] = useState(provider.default_model ?? '')
  const [addingKey, setAddingKey] = useState(false)
  const [addConnFailed, setAddConnFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const { connectingId, startOAuthConnect, notice } = useProviderOAuthConnect(onRefresh)

  const label = badgeLabel(provider)
  const displayLabel = badgeDisplayLabel(label, t)
  const kindColor = KIND_COLORS[label.toLowerCase()] ?? 'var(--color-text-dim)'
  const name = providerName(provider)
  const id = provider.provider_id ?? ''

  // Cloud-managed providers are owned by the org's Enterprise policy.
  // The REST layer enforces this; we reflect it here: no delete/re-key allowed.
  const isCloudManaged = provider.managed_by === 'cloud'

  const isActive = isConfigured && provider.is_active
  const oauthPending = connectingId === id

  async function handleActivate() {
    if (inFlight.current || isCloudManaged) return
    inFlight.current = true; setBusy(true)
    try {
      await setActiveProvider(id)
      if (!alive.current) return
      onToast(t('providers.toast.activated').replace('{name}', name), 'ok')
      onRefresh()
    } catch { if (alive.current) onToast(t('providers.err.generic'), 'error') }
    finally { inFlight.current = false; if (alive.current) setBusy(false) }
  }

  async function handleTest() {
    if (inFlight.current) return
    inFlight.current = true; setBusy(true)
    setTesting(true)
    try {
      const r = await testProvider(id)
      if (!alive.current) return
      // PROV-03: r.error is the provider's own honest reason (invalid key,
      // wrong endpoint...) once ok is false — show it instead of a generic
      // "failed" toast so the owner knows whether to fix the key or the URL.
      const message = r?.ok ? t('providers.test.ok') : t('providers.test.fail')
      onToast(message, r?.ok ? 'ok' : 'warn')
    } catch { if (alive.current) onToast(t('providers.err.generic'), 'error') }
    finally { inFlight.current = false; if (alive.current) { setTesting(false); setBusy(false) } }
  }

  async function handleDelete() {
    if (inFlight.current || isCloudManaged) return
    inFlight.current = true; setBusy(true)
    try {
    const ok = await onConfirm({
      title: t('providers.delete.confirm.title').replace('{name}', name),
      description: isActive
        ? t('providers.delete.confirm.desc_active')
        : undefined,
      confirmLabel: t('providers.delete'),
      variant: 'danger',
    })
    if (!ok || !alive.current) return
      await deleteProvider(id)
      if (!alive.current) return
      onToast(t('providers.toast.deleted'), 'ok')
      onRefresh()
    } catch { if (alive.current) onToast(t('providers.err.generic'), 'error') }
    finally { inFlight.current = false; if (alive.current) setBusy(false) }
  }

  async function handleAddConfirm() {
    if (inFlight.current) return
    if (!apiKeyInput.trim()) { onToast(t('providers.err.enter_key'), 'warn'); return }
    // PROV-02: a provider saved with no model leaves config.yaml with
    // model.provider set and no model.default — the first chat then dies
    // with HermesModelNotConfiguredError instead of failing here, clearly.
    if (!modelInput.trim()) { onToast(t('providers.err.enter_model'), 'warn'); return }
    inFlight.current = true; setBusy(true); setAddingKey(true)
    try {
      // Native catalogue providers go through /providers/native by their registry
      // provider_id (the daemon resolves env var + default model). Sending `kind`
      // here left provider_id empty → "provider desconocido".
      const created = await configureNativeProvider({
        provider_id: provider.provider_id ?? id,
        api_key: apiKeyInput.trim(),
        model: modelInput.trim(),
        set_active: false,
      })
      if (!alive.current) return
      const realId = created?.provider_id || id
      setShowKeyForm(false)
      setApiKeyInput('')

      let testPassed = false
      try {
        const r = await testProvider(realId)
        testPassed = r?.ok === true
      } catch {
        testPassed = false
      }
      if (!alive.current) return

      if (testPassed) {
        await setActiveProvider(realId)
        setAddConnFailed(false)
        onToast(t('providers.toast.connected_verified').replace('{name}', name), 'ok')
        onRefresh()
      } else {
        setAddConnFailed(true)
        onRefresh()
      }
    } catch (e) {
      if (!alive.current) return
      if (isOAuthRequiredError(e)) {
        // This native row needs OAuth (its registry auth_type isn't api_key) —
        // pivot straight to the device-code flow instead of surfacing the raw
        // "oauth_required" error string.
        setShowKeyForm(false)
        setApiKeyInput('')
        onToast(t('providers.oauth.fallback_notice').replace('{name}', name), 'ok')
        void startOAuthConnect(provider.provider_id ?? id, name)
        return
      }
      onToast(t('providers.err.generic'), 'error')
    } finally {
      inFlight.current = false
      if (alive.current) { setAddingKey(false); setBusy(false) }
    }
  }

  function handleOAuth() {
    void startOAuthConnect(id, name)
  }

  const rowClass = [css.row, isActive ? css.rowActive : ''].filter(Boolean).join(' ')

  return (
    <motion.div
      className={rowClass}
      transition={SPRING}
      layout
    >
      <ProviderTypeChip provider={provider} />

      <div className={css.rowLeft}>
        <span className={css.rowName}>{name}</span>
        <OAuthNotice notice={notice} />
        <div className={css.rowMeta}>
          {/* Per-kind colour pill — CSS custom property set inline */}
          <span
            className={css.kindBadge}
            style={{ '--kind-color': kindColor } as React.CSSProperties}
          >
            {displayLabel}
          </span>

          {provider.default_model && (
            <span className={css.modelString} title={provider.default_model}>
              {provider.default_model}
            </span>
          )}

          {isActive && (
            <Badge variant="ok">{t('providers.active')}</Badge>
          )}

          {isCloudManaged && (
            <Badge variant="neutral">{t('providers.managed_by_org')}</Badge>
          )}

          {addConnFailed && (
            <Badge variant="danger">
              <span role="alert">{t('providers.conn_failed')}</span>
            </Badge>
          )}
        </div>
      </div>

      <div className={css.rowActions}>
        {isConfigured ? (
          <>
            {!provider.is_active && !isCloudManaged && (
              <Button variant="secondary" size="sm" onClick={handleActivate} disabled={busy}>
                {t('providers.activate')}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={handleTest}
              disabled={busy}
              loading={testing}
            >
              {testing ? t('providers.testing') : t('providers.test')}
            </Button>
            {!isCloudManaged && (
              <Button
                variant="danger"
                size="sm"
                onClick={handleDelete}
                disabled={busy}
                aria-label={t('providers.delete.aria').replace('{name}', name)}
              >
                {t('providers.delete')}
              </Button>
            )}
          </>
        ) : isOAuthProvider(provider) ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={handleOAuth}
            disabled={oauthPending}
            loading={oauthPending}
          >
            {oauthPending ? t('providers.connecting') : t('providers.connect')}
          </Button>
        ) : !showKeyForm ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => { setShowKeyForm(true); setAddConnFailed(false) }}
          >
            {addConnFailed ? t('providers.retry_key') : t('providers.add')}
          </Button>
        ) : null}
      </div>

      {/* Animated inline API-key form */}
      <AnimatedExpanderContent open={!isConfigured && !isOAuthProvider(provider) && showKeyForm}>
        <div className={css.keyForm}>
          <label className="sr-only" htmlFor={`pv-key-${id}`}>
            {t('providers.key.label').replace('{name}', name)}
          </label>
          <input
            id={`pv-key-${id}`}
            className={css.keyInput}
            type="password"
            autoComplete="new-password"
            placeholder={t('providers.key.label').replace('{name}', name)}
            value={apiKeyInput}
            onChange={e => setApiKeyInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') void handleAddConfirm() }}
          />
          <label className="sr-only" htmlFor={`pv-model-${id}`}>
            {t('providers.model.label').replace('{name}', name)}
          </label>
          <input
            id={`pv-model-${id}`}
            className={css.keyInput}
            type="text"
            placeholder={t('providers.model.label').replace('{name}', name)}
            value={modelInput}
            onChange={e => setModelInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') void handleAddConfirm() }}
          />
          <motion.div
            style={{ display: 'contents' }}
            initial={false}
          >
            <Button
              variant="primary"
              size="sm"
              onClick={handleAddConfirm}
              disabled={addingKey}
              loading={addingKey}
            >
              {addingKey ? t('providers.saving') : t('providers.save')}
            </Button>
          </motion.div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => { setShowKeyForm(false); setApiKeyInput('') }}
          >
            {t('providers.cancel')}
          </Button>
        </div>
      </AnimatedExpanderContent>
    </motion.div>
  )
}

// ── Custom provider card (OpenAI-compatible local/remote model) ───────────────

interface CustomProviderCardProps {
  onAdded: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function CustomProviderCard({ onAdded, onToast }: CustomProviderCardProps) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [connFailed, setConnFailed] = useState(false)
  const reduced = useReducedMotion()
  const aliasRef = useRef<HTMLInputElement>(null)
  const urlRef = useRef<HTMLInputElement>(null)
  const modelRef = useRef<HTMLInputElement>(null)
  const keyRef = useRef<HTMLInputElement>(null)
  const pending = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  async function handleSave() {
    if (pending.current) return
    const base_url = urlRef.current?.value.trim() ?? ''
    const default_model = modelRef.current?.value.trim() ?? ''
    const alias = aliasRef.current?.value.trim() || default_model || t('providers.custom.default_alias')
    const api_key = keyRef.current?.value.trim() || undefined

    if (!base_url || !default_model) {
      onToast(t('providers.custom.err.required'), 'warn')
      return
    }

    pending.current = true; setSaving(true)
    try {
      const added = await addProvider({ kind: 'openai_compatible', alias, default_model, base_url, api_key, set_active:false })
      if (!alive.current) return
      const newId = (added as { provider_id?: string }).provider_id ?? alias

      let testPassed = false
      try {
        const r = await testProvider(newId)
        testPassed = r?.ok === true
      } catch {
        testPassed = false
      }
      if (!alive.current) return

      if (testPassed) {
        await setActiveProvider(newId)
        if (!alive.current) return
        setConnFailed(false)
        setOpen(false)
        if (aliasRef.current) aliasRef.current.value = ''
        if (urlRef.current) urlRef.current.value = ''
        if (modelRef.current) modelRef.current.value = ''
        if (keyRef.current) keyRef.current.value = ''
        onToast(t('providers.custom.toast.added'), 'ok')
        onAdded()
      } else {
        setConnFailed(true)
        onAdded()
      }
    } catch { if (alive.current) onToast(t('providers.err.generic'), 'error') }
    finally { pending.current = false; if (alive.current) setSaving(false) }
  }

  return (
    <motion.div className={css.customCard} layout>
      <div className={css.customCardHeader}>
        <p className={css.customCardIntro}>
          {t('providers.custom.intro')}
        </p>
        {!open && (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setOpen(true)}
            style={{ alignSelf: 'flex-start', flexShrink: 0 }}
          >
            {t('providers.custom.add_btn')}
          </Button>
        )}
      </div>

      <AnimatedExpanderContent open={open}>
        <div className={css.formStack}>
          <div className={css.formField}>
            <label className={css.formLabel} htmlFor="pv-c-alias">{t('providers.custom.name.label')}</label>
            <input
              id="pv-c-alias"
              ref={aliasRef}
              className={css.formInput}
              type="text"
              placeholder={t('providers.custom.name.placeholder')}
              autoComplete="off"
            />
          </div>

          <div className={css.formField}>
            <label className={css.formLabel} htmlFor="pv-c-url">{t('providers.custom.url.label')}</label>
            <input
              id="pv-c-url"
              ref={urlRef}
              className={css.formInput}
              type="text"
              placeholder={t('providers.custom.url.placeholder')}
              autoComplete="off"
            />
          </div>

          <div className={css.formField}>
            <label className={css.formLabel} htmlFor="pv-c-model">{t('providers.custom.model.label')}</label>
            <input
              id="pv-c-model"
              ref={modelRef}
              className={css.formInput}
              type="text"
              placeholder="qwen3.6-35b-a3b"
              autoComplete="off"
            />
          </div>

          <div className={css.formField}>
            <label className={css.formLabel} htmlFor="pv-c-key">
              {t('providers.custom.key.label')}{' '}
              <span style={{ fontWeight: 400, color: 'var(--color-text-dim)' }}>({t('providers.optional')})</span>
            </label>
            <input
              id="pv-c-key"
              ref={keyRef}
              className={css.formInput}
              type="password"
              placeholder={t('providers.custom.key.placeholder')}
              autoComplete="new-password"
            />
          </div>

          <p className={css.formHint}>
            {t('providers.custom.hint_pre')} <code style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>/v1</code>{t('providers.custom.hint_post')}
          </p>

          {connFailed && (
            <motion.div
              className={css.connError}
              role="alert"
              initial={reduced ? false : { opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={TWEEN_FAST}
            >
              <AlertCircle size={14} style={{ flexShrink: 0, marginTop: 1 }} aria-hidden="true" />
              <span>
                {t('providers.custom.conn_failed')}
              </span>
            </motion.div>
          )}

          <div className={css.formActions}>
            <Button
              variant="primary"
              size="sm"
              onClick={handleSave}
              disabled={saving}
              loading={saving}
            >
              {saving ? t('providers.saving') : connFailed ? t('providers.custom.retry_conn') : t('providers.custom.save_activate')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { setOpen(false); setConnFailed(false) }}
              disabled={saving}
            >
              {t('providers.cancel')}
            </Button>
          </div>
        </div>
      </AnimatedExpanderContent>
    </motion.div>
  )
}

// ── OpenAI Codex / ChatGPT (suscripción) onboarding card ───────────────────────
//
// Two independent auth paths, both explained up front so the owner picks the
// one that matches how they pay for Codex — a ChatGPT subscription (no key,
// device-code login) or their own OpenAI API key (pay-per-token fallback,
// plan.md D-A4). The device-code button reuses the SAME OAuth state machine
// as the native-catalogue "Connect" button (useProviderOAuthConnect); the key
// path goes through the generic add_provider flow with kind="openai_codex" —
// NOT /providers/native, which would reject an api_key for this provider_id
// (its registry auth_type isn't "api_key", see isOAuthRequiredError).

interface CodexProviderCardProps {
  onAdded: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function CodexProviderCard({ onAdded, onToast }: CodexProviderCardProps) {
  const t = useT()
  const [model, setModel] = useState<string>(CODEX_DEFAULT_MODEL)
  const [showKeyForm, setShowKeyForm] = useState(false)
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [savingKey, setSavingKey] = useState(false)
  const pending = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const { connectingId, startOAuthConnect, notice } = useProviderOAuthConnect(onAdded)
  const oauthPending = connectingId === CODEX_PROVIDER_ID

  async function handleApiKeySave() {
    if (pending.current || oauthPending) return
    if (!apiKeyInput.trim()) { onToast(t('providers.err.enter_key'), 'warn'); return }
    pending.current = true; setSavingKey(true)
    try {
      const created = await addProvider({
        kind: CODEX_KIND,
        alias: t('providers.codex.alias'),
        default_model: model,
        api_key: apiKeyInput.trim(),
        set_active: false,
      })
      if (!alive.current) return
      const newId = (created as { provider_id?: string }).provider_id
      setApiKeyInput('')
      setShowKeyForm(false)

      let testPassed = false
      if (newId) {
        try {
          const r = await testProvider(newId)
          testPassed = r?.ok === true
        } catch {
          testPassed = false
        }
      }
      if (!alive.current) return
      if (testPassed && newId) await setActiveProvider(newId)
      if (!alive.current) return
      onToast(
        testPassed
          ? t('providers.toast.connected_verified').replace('{name}', t('providers.codex.alias'))
          : t('providers.custom.conn_failed'),
        testPassed ? 'ok' : 'warn',
      )
      onAdded()
    } catch {
      if (alive.current) onToast(t('providers.err.generic'), 'error')
    } finally {
      pending.current = false
      if (alive.current) setSavingKey(false)
    }
  }

  return (
    <motion.div className={css.customCard} layout>
      <div className={css.customCardHeader}>
        <p className={css.customCardIntro}>{t('providers.codex.explain')}</p>
      </div>
      <OAuthNotice notice={notice} />

      <div className={css.formStack}>
        <div className={css.formField}>
          <label className={css.formLabel} htmlFor="pv-codex-model">
            {t('providers.codex.model.label')}
          </label>
          <select
            id="pv-codex-model"
            className={css.formInput}
            value={model}
            onChange={e => setModel(e.target.value)}
          >
            {CODEX_MODELS.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
          <p className={css.formHint}>{t('providers.codex.model_scope')}</p>
        </div>

        <div className={css.formActions}>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void startOAuthConnect(CODEX_PROVIDER_ID, t('providers.codex.alias'))}
            disabled={oauthPending || savingKey}
            loading={oauthPending}
          >
            {oauthPending ? t('providers.connecting') : t('providers.codex.login_btn')}
          </Button>
          {!showKeyForm && (
            <Button variant="ghost" size="sm" onClick={() => setShowKeyForm(true)}>
              {t('providers.codex.use_key_btn')}
            </Button>
          )}
        </div>

        <AnimatedExpanderContent open={showKeyForm}>
          <div className={css.formStack} style={{ borderTop: 'none', paddingTop: 0 }}>
            <div className={css.formField}>
              <label className={css.formLabel} htmlFor="pv-codex-key">
                {t('providers.custom.key.label')}
              </label>
              <input
                id="pv-codex-key"
                className={css.formInput}
                type="password"
                autoComplete="new-password"
                placeholder={t('providers.codex.key.placeholder')}
                value={apiKeyInput}
                onChange={e => setApiKeyInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') void handleApiKeySave() }}
              />
            </div>
            <div className={css.formActions}>
              <Button
                variant="primary"
                size="sm"
                onClick={handleApiKeySave}
                disabled={savingKey}
                loading={savingKey}
              >
                {savingKey ? t('providers.saving') : t('providers.save')}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => { setShowKeyForm(false); setApiKeyInput('') }}
              >
                {t('providers.cancel')}
              </Button>
            </div>
          </div>
        </AnimatedExpanderContent>
      </div>
    </motion.div>
  )
}
