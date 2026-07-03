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
  FadeIn,
  Stagger,
  StaggerItem,
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
    <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      {[...Array(count)].map((_, i) => (
        <StaggerItem key={i}>
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
        </StaggerItem>
      ))}
    </Stagger>
  )
}

// ── Main view ─────────────────────────────────────────────────────────────────

export default function ProvidersView() {
  const t = useT()
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  function load() {
    dispatch({ type: 'RELOAD' })
    Promise.all([listProviders(), listNativeProviders(), getNativeActive()])
      .then(([configured, native, nativeActive]) => {
        const cfg = Array.isArray(configured) ? configured : []
        // Native-configured providers live in a separate store from the repo;
        // surface the active one in the configured list so a just-added native
        // catalogue provider is actually visible + marked active.
        const merged = nativeActive && !cfg.some(p => p.provider_id === nativeActive.provider_id)
          ? [nativeActive, ...cfg]
          : cfg
        dispatch({
          type: 'LOADED',
          configured: merged,
          native: Array.isArray(native) ? native : [],
        })
      })
      .catch((err: unknown) => {
        dispatch({
          type: 'FAILED',
          message: err instanceof ApiError ? err.message : t('providers.err.load'),
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
      <PageHeader
        title={t('providers.title')}
        subtitle={t('providers.subtitle')}
      />

      <div className={`view-body ${css.body}`}>
        {state.status === 'loading' && (
          <section className={css.section} aria-label={t('providers.loading_aria')}>
            <div className={css.sectionLabel} aria-hidden="true">{t('providers.section.configured')}</div>
            <SkeletonRows count={3} />
          </section>
        )}

        {state.status === 'error' && (
          <FadeIn>
            <div className={css.errorBox} role="alert">
              <AlertCircle size={16} style={{ color: 'var(--color-danger)', flexShrink: 0, marginTop: 1 }} aria-hidden="true" />
              <span className={css.errorText}>{state.message}</span>
              <Button variant="secondary" size="sm" onClick={load}>
                {t('providers.retry')}
              </Button>
            </div>
          </FadeIn>
        )}

        {state.status === 'success' && (
          <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>

            {/* ── Configured providers ── */}
            <StaggerItem>
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
                        document.getElementById('pv-catalogue')?.scrollIntoView({ behavior: 'smooth' })
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
            </StaggerItem>

            {/* ── Custom / local model ── */}
            <StaggerItem>
              <section className={css.section} aria-label={t('providers.section.custom.aria')}>
                <h2 className={css.sectionLabel}>{t('providers.section.custom')}</h2>
                <CustomProviderCard onAdded={load} onToast={show} />
              </section>
            </StaggerItem>

            {/* ── Native Hermes catalogue ── */}
            <StaggerItem>
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
                        .filter(p => !configuredIds.has(p.provider_id))
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
            </StaggerItem>

          </Stagger>
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
  const t = useT()
  const reduced = useReducedMotion()
  const [testing, setTesting] = useState(false)
  const [oauthPending, setOauthPending] = useState(false)
  const [showKeyForm, setShowKeyForm] = useState(false)
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [addingKey, setAddingKey] = useState(false)
  const [addConnFailed, setAddConnFailed] = useState(false)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const label = badgeLabel(provider)
  const displayLabel = badgeDisplayLabel(label, t)
  const kindColor = KIND_COLORS[label.toLowerCase()] ?? 'var(--color-text-dim)'
  const name = providerName(provider)
  const id = provider.provider_id ?? ''

  // Cloud-managed providers are owned by the org's Enterprise policy.
  // The REST layer enforces this; we reflect it here: no delete/re-key allowed.
  const isCloudManaged = provider.managed_by === 'cloud'

  const isActive = isConfigured && provider.is_active

  async function handleActivate() {
    try {
      await setActiveProvider(id)
      onToast(t('providers.toast.activated').replace('{name}', name), 'ok')
      onRefresh()
    } catch (e) {
      onToast(e instanceof Error ? e.message : t('providers.err.generic'), 'error')
    }
  }

  async function handleTest() {
    setTesting(true)
    try {
      const r = await testProvider(id)
      onToast(r?.ok ? t('providers.test.ok') : t('providers.test.fail'), r?.ok ? 'ok' : 'warn')
    } catch (e) {
      onToast(e instanceof Error ? e.message : t('providers.err.generic'), 'error')
    } finally { setTesting(false) }
  }

  async function handleDelete() {
    const ok = await onConfirm({
      title: t('providers.delete.confirm.title').replace('{name}', name),
      description: isActive
        ? t('providers.delete.confirm.desc_active')
        : undefined,
      confirmLabel: t('providers.delete'),
      variant: 'danger',
    })
    if (!ok) return
    try {
      await deleteProvider(id)
      onToast(t('providers.toast.deleted'), 'ok')
      onRefresh()
    } catch (e) {
      onToast(e instanceof Error ? e.message : t('providers.err.generic'), 'error')
    }
  }

  async function handleAddConfirm() {
    if (!apiKeyInput.trim()) { onToast(t('providers.err.enter_key'), 'warn'); return }
    setAddingKey(true)
    try {
      // Native catalogue providers go through /providers/native by their registry
      // provider_id (the daemon resolves env var + default model). Sending `kind`
      // here left provider_id empty → "provider desconocido".
      const created = await configureNativeProvider({
        provider_id: provider.provider_id ?? id,
        api_key: apiKeyInput.trim(),
      })
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
      onToast(e instanceof Error ? e.message : t('providers.err.generic'), 'error')
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
      onToast(e instanceof Error ? e.message : t('providers.oauth.err.connect'), 'error')
      setOauthPending(false)
      return
    }

    if (!r || r['error']) {
      onToast(t('providers.oauth.err.connect_reason').replace('{reason}', (r?.['error'] as string) ?? t('providers.err.unknown')), 'error')
      setOauthPending(false)
      return
    }

    const session = r['session_id'] as string | undefined
    const url = (r['auth_url'] ?? r['verification_url']) as string | undefined
    const code = r['user_code'] as string | undefined

    if (url) {
      window.open(url, '_blank', 'noopener,noreferrer')
      onToast(t('providers.oauth.opening').replace('{name}', name), 'ok')
    }
    if (code) {
      onToast(t('providers.oauth.go_and_code').replace('{url}', url ?? '').replace('{code}', code), 'ok')
    } else {
      onToast(t('providers.oauth.waiting').replace('{name}', name), 'ok')
    }

    if (!session) { setOauthPending(false); return }

    const intervalMs = Math.max(2000, ((r['poll_interval'] as number | undefined) ?? 4) * 1000)
    const deadline = Date.now() + Math.max(60, ((r['expires_in'] as number | undefined) ?? 600)) * 1000

    const poll = async () => {
      if (Date.now() > deadline) {
        onToast(t('providers.oauth.expired'), 'warn')
        setOauthPending(false)
        return
      }
      const st = await getProviderOAuthStatus(session)
      const status = String(st?.status ?? '').toLowerCase()
      if (status === 'approved' || status === 'connected' || status === 'success') {
        onToast(t('providers.oauth.connected').replace('{name}', name), 'ok')
        setOauthPending(false)
        onRefresh()
        return
      }
      if (status === 'error' || status === 'failed') {
        onToast(t('providers.oauth.err.connect_reason').replace('{reason}', String(st?.error_message ?? st?.error ?? t('providers.err.unknown'))), 'error')
        setOauthPending(false)
        return
      }
      if (status === 'expired') {
        onToast(t('providers.oauth.expired'), 'warn')
        setOauthPending(false)
        return
      }
      pollRef.current = setTimeout(poll, intervalMs)
    }
    pollRef.current = setTimeout(poll, intervalMs)
  }

  useEffect(() => () => {
    if (pollRef.current) clearTimeout(pollRef.current)
  }, [])

  const rowClass = [css.row, isActive ? css.rowActive : ''].filter(Boolean).join(' ')

  return (
    <motion.div
      className={rowClass}
      whileHover={reduced ? undefined : { y: -2 }}
      transition={SPRING}
      layout
    >
      <ProviderTypeChip provider={provider} />

      <div className={css.rowLeft}>
        <span className={css.rowName}>{name}</span>
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
              <Button variant="secondary" size="sm" onClick={handleActivate}>
                {t('providers.activate')}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={handleTest}
              disabled={testing}
              loading={testing}
            >
              {testing ? t('providers.testing') : t('providers.test')}
            </Button>
            {!isCloudManaged && (
              <Button
                variant="danger"
                size="sm"
                onClick={handleDelete}
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

  async function handleSave() {
    const base_url = urlRef.current?.value.trim() ?? ''
    const default_model = modelRef.current?.value.trim() ?? ''
    const alias = aliasRef.current?.value.trim() || default_model || t('providers.custom.default_alias')
    const api_key = keyRef.current?.value.trim() || undefined

    if (!base_url || !default_model) {
      onToast(t('providers.custom.err.required'), 'warn')
      return
    }

    setSaving(true)
    try {
      const added = await addProvider({ kind: 'openai_compatible', alias, default_model, base_url, api_key })
      const newId = (added as { provider_id?: string }).provider_id ?? alias

      let testPassed = false
      try {
        const r = await testProvider(newId)
        testPassed = r?.ok === true
      } catch {
        testPassed = false
      }

      if (testPassed) {
        await setActiveProvider(newId)
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
    } catch (e) {
      onToast(e instanceof Error ? e.message : t('providers.err.generic'), 'error')
    } finally { setSaving(false) }
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
            >
              {t('providers.cancel')}
            </Button>
          </div>
        </div>
      </AnimatedExpanderContent>
    </motion.div>
  )
}
