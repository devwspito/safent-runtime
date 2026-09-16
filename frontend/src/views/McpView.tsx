import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { X, Terminal, Search, Wrench, ExternalLink, Megaphone, Link2, Lightbulb } from 'lucide-react'
import { useT, useLocale } from '../lib/i18n'
import type { TranslationKey } from '../lib/i18n'
import {
  listMcpServers, addMcpServer, removeMcpServer, searchMcpRegistry, scanInstall, recordSecurityDecision,
  listManagedRemoteEndpoints, connectManagedRemote, ApiError,
} from '../api/client'
import type { McpServer, McpRegistryEntry, InstallScanResponse } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'
import InstallScanModal from '../components/InstallScanModal'
import { panelOriginFromMcpUrl } from '../hooks/useAdsPanel'
import { useAdsAvailability } from '../hooks/useAdsAvailability'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import { Badge as DsBadge, StatusDot } from '../components/ui/Badge'
import type { StatusDotState } from '../components/ui/Badge'
import { CompanionInstallAction } from '../components/CompanionInstallAction'
import {
  AnimatePresence,
  AnimatedListItem,
  AnimatedExpanderContent,
  AnimatedChevron,
  HoverRow,
  motion,
  SPRING,
  TWEEN_FAST,
} from '../components/ui/motion'
import styles from './McpView.module.css'

// Curated catalog of verified one-click MCP servers (npx/uvx).
function mcpCatalog(t: ReturnType<typeof useT>): McpRegistryEntry[] {
  return [
    {
      server_id: 'github',
      label: 'GitHub',
      tag: t('mcp.catalog.tag.dev'),
      description: t('mcp.catalog.github.desc'),
      argv: ['npx', '-y', '@modelcontextprotocol/server-github'],
      repository: 'https://github.com/github/github-mcp-server',
    },
    {
      server_id: 'context7',
      label: 'Context7',
      tag: t('mcp.catalog.tag.docs'),
      description: t('mcp.catalog.context7.desc'),
      argv: ['npx', '-y', '@upstash/context7-mcp'],
      repository: 'https://github.com/upstash/context7',
    },
    {
      server_id: 'filesystem',
      label: t('mcp.catalog.filesystem.label'),
      tag: t('mcp.catalog.tag.system'),
      description: t('mcp.catalog.filesystem.desc'),
      argv: ['npx', '-y', '@modelcontextprotocol/server-filesystem', '/var/lib/hermes/workspace'],
      repository: 'https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem',
    },
  ]
}

function slugify(name: string): string {
  return String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'herramienta'
}

function getRunner(argv: string | string[] | undefined): string {
  const arr = Array.isArray(argv)
    ? argv
    : String(argv ?? '').split(/\s+/).filter(Boolean)
  return (arr[0] ? String(arr[0]) : '')
    .split(/[/\s]+/)
    .filter(Boolean)
    .pop() ?? ''
}

// Resolve the FETCHABLE registry coordinate ("npm:@scope/pkg" or "pypi:pkg") from a
// runner argv, so the security scan can download + statically analyse the ACTUAL
// package. Without this the scan only sees the display name (no registry coordinate)
// → PackageContentScanner has nothing to fetch → every MCP gets the same constant
// score. Handles BOTH ecosystems (this was npx-only before, so every uvx/pip MCP fell
// back to the display name and got the bogus constant score). Returns null only for a
// truly non-fetchable runner (docker/local/inline) — and the backend treats a null
// coordinate as "code not verifiable", NOT a clean PASS.
function fetchableCoordinateFromArgv(argv: string | string[] | undefined): string | null {
  const arr = Array.isArray(argv)
    ? argv
    : String(argv ?? '').split(/\s+/).filter(Boolean)
  if (!arr.length) return null
  const runner = getRunner(arr)
  // npx → npm ; uvx/uv/pipx/pip → pypi (mirrors the daemon's _NPM_RUNNERS/_PYPI_RUNNERS)
  const eco = runner === 'npx' ? 'npm'
    : (runner === 'uvx' || runner === 'uv' || runner === 'pipx' || runner === 'pip') ? 'pypi'
    : null
  if (!eco) return null
  for (let i = 1; i < arr.length; i++) {
    const tok = arr[i]!
    if (tok.startsWith('-')) continue            // skip flags (-y, --yes, --from, run, ...)
    if (tok === 'run' || tok === 'tool') continue // uv run / uv tool run noise
    if (/[/\\]/.test(tok) && !tok.startsWith('@')) return null  // local path, not a pkg
    return `${eco}:${tok}`                        // [@scope/]name[@version]
  }
  return null
}

// EnvField schema derived from entry.env_vars
interface EnvFieldSchema {
  key: string
  label: string
  required: boolean
  secret: boolean
}

function parseEnvSchema(entry: McpRegistryEntry): EnvFieldSchema[] {
  const rawVars = entry.env_vars ?? []
  return rawVars.map(v =>
    typeof v === 'string'
      ? { key: v, label: v, required: false, secret: true }
      : { key: v.key, label: v.label ?? v.key, required: Boolean(v.required), secret: Boolean(v.secret ?? true) },
  )
}

// ── Safent Ads managed-remote preset ────────────────────────────────────────
//
// A managed-remote MCP server (safent-ads) bridges to Safent's own ads
// control plane via mcp-remote — the owner sets ONE https URL (their tenant's
// endpoint), never a local command. Mirrors the backend's validation
// (hermes.shell_server.managed_remote_endpoints.validate_managed_remote_endpoint_url)
// for instant feedback; the backend re-validates regardless (client-side
// checks are UX only, never the trust boundary).

const SAFENT_ADS_SLUG = 'safent-ads'
// The one fetchable coordinate the install security-scan analyses (the URL is
// a runtime argv value, not code — mirrors fetchableCoordinateFromArgv's own
// npx→npm resolution for ['npx', '-y', 'mcp-remote', url]).
const SAFENT_ADS_SCAN_TARGET = 'npm:mcp-remote'
const SAFENT_ADS_BLOCKED_HOSTNAMES = new Set(['localhost', 'metadata.google.internal', 'metadata'])

function isIpLiteralHostname(hostname: string): boolean {
  const bare = hostname.replace(/^\[/, '').replace(/\]$/, '')
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(bare)) return true
  return bare.includes(':')
}

function validateManagedRemoteUrl(t: ReturnType<typeof useT>, raw: string): string | null {
  const value = raw.trim()
  if (!value) return t('mcp.managed.err.required')
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return t('mcp.managed.err.invalid')
  }
  if (parsed.protocol !== 'https:') return t('mcp.managed.err.scheme')
  const hostname = parsed.hostname.toLowerCase()
  if (!hostname) return t('mcp.managed.err.hostname')
  if (SAFENT_ADS_BLOCKED_HOSTNAMES.has(hostname)) return t('mcp.managed.err.blocked_hostname')
  if (isIpLiteralHostname(hostname)) return t('mcp.managed.err.ip_literal')
  if (parsed.port && parsed.port !== '443') return t('mcp.managed.err.port')
  return null
}

// ── State ─────────────────────────────────────────────────────────────────────

type State =
  | { status: 'loading' }
  | { status: 'success'; servers: McpServer[] }
  | { status: 'error'; message: string }

type Action =
  | { type: 'LOADING' }
  | { type: 'LOADED'; servers: McpServer[] }
  | { type: 'FAILED'; message: string }

function reducer(_s: State, a: Action): State {
  switch (a.type) {
    case 'LOADING': return { status: 'loading' }
    case 'LOADED': return { status: 'success', servers: a.servers }
    case 'FAILED': return { status: 'error', message: a.message }
  }
}

// Registry search — separate discriminated state so the main list stays intact
type RegistryState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; results: McpRegistryEntry[] }
  | { status: 'error'; message: string }

function show(message: string, kind: 'ok' | 'warn' | 'error' = 'ok', durationMs = 4000) {
  if (kind === 'ok') sileo.success({ title: message, duration: durationMs })
  else if (kind === 'error') sileo.error({ title: message, duration: durationMs })
  else sileo.warning({ title: message, duration: durationMs })
}

// Pending install approval: holds the scan result + pending install entry
interface PendingInstall {
  scan: InstallScanResponse
  entry: McpRegistryEntry
  collectedEnv: Record<string, string>
  onDone: () => void
}

export default function McpView() {
  const t = useT()
  const MCP_CATALOG = mcpCatalog(t)
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })
  const [registryState, setRegistryState] = useState<RegistryState>({ status: 'idle' })
  const [pendingInstall, setPendingInstall] = useState<PendingInstall | null>(null)
  const regInputRef = useRef<HTMLInputElement>(null)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()
  const loadGeneration = useRef(0)
  const searchGeneration = useRef(0)
  const alive = useRef(true)

  function load() {
    const request = ++loadGeneration.current
    dispatch({ type: 'LOADING' })
    listMcpServers()
      // Ruflo is a first-class Safent integration, not a user-managed tool set.
      // The backend already hides it but we filter defensively client-side too.
      .then(servers => {
        if (request !== loadGeneration.current) return
        if (!Array.isArray(servers)) throw new Error('invalid MCP list')
        dispatch({ type: 'LOADED', servers: servers.filter(s => s.slug !== 'ruflo') })
      })
      .catch(() => { if (request === loadGeneration.current) dispatch({
        type: 'FAILED',
        message: t('mcp.err.load'),
      }) })
  }

  useEffect(() => { alive.current = true; load(); return () => { alive.current = false; loadGeneration.current++; searchGeneration.current++ } }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const installedIds = state.status === 'success'
    ? new Set(state.servers.map(s => s.server_id ?? s.id ?? ''))
    : new Set<string>()

  const safentAdsServer = state.status === 'success'
    ? state.servers.find(s => (s.server_id ?? s.id) === SAFENT_ADS_SLUG)
    : undefined

  async function handleRemoveServer(s: McpServer) {
    const name = s.label ?? s.server_id ?? ''
    const ok = await confirm({
      title: t('mcp.remove.confirm.title').replace('{name}', name),
      description: t('mcp.remove.confirm.desc'),
      confirmLabel: t('mcp.remove'),
      variant: 'danger',
    })
    if (!ok) return
    try {
      await removeMcpServer(s.server_id ?? s.id ?? '')
      show(t('mcp.toast.removed'), 'ok')
      load()
    } catch (e) {
      show(e instanceof Error ? e.message : t('mcp.err.generic'), 'error')
    }
  }

  async function doAddMcpServer(entry: McpRegistryEntry, collectedEnv: Record<string, string>, onDone: () => void, force = false, approvalGrant?: string) {
    const argv = Array.isArray(entry.argv)
      ? entry.argv
      : String(entry.argv ?? '').split(/\s+/).filter(Boolean)

    try {
      const res = await addMcpServer({
        server_id: entry.server_id ?? entry.id ?? slugify(entry.name ?? ''),
        label: entry.label ?? entry.name,
        argv,
        env: { ...collectedEnv },
        // Only the one-use grant authorizes this exact owner-reviewed draft.
        force,
      }, approvalGrant)
      const name = entry.label ?? entry.name ?? ''
      if (res && res.tool_count === 0) {
        show(t('mcp.toast.no_tools').replace('{name}', name), 'warn', 7000)
      } else {
        show(t('mcp.toast.added').replace('{name}', name), 'ok')
      }
      load()
    } catch (e) {
      show(e instanceof Error ? e.message : t('mcp.err.generic'), 'error')
    } finally {
      onDone()
    }
  }

  async function installEntry(entry: McpRegistryEntry, collectedEnv: Record<string, string>, onDone: () => void) {
    if (state.status !== 'success') { show(t('mcp.err.load'), 'error'); onDone(); return }
    // npx (npm) and uvx (PyPI) both resolve to a published package the content +
    // CVE scanners can fetch and statically analyze, so the verdict is REAL. Other
    // runners (local node/python3 scripts, inline commands) have no inspectable
    // coordinate → kept out of the one-click path; the backend scan also treats an
    // MCP with no published package as non-analyzable (owner review, never PASS).
    const runner = getRunner(entry.argv)
    const ALLOWED_RUNNERS = ['npx', 'uvx']
    if (runner && !ALLOWED_RUNNERS.includes(runner)) {
      show(t('mcp.unsupported_runner').replace('{runner}', runner), 'warn', 7000)
      onDone()
      return
    }

    const identifier = entry.server_id ?? entry.id ?? slugify(entry.name ?? '')
    // Scan the FETCHABLE coordinate (npm:@scope/pkg) when we can resolve it, so the
    // content scanner downloads + analyses the real package and the verdict is REAL
    // (a malicious package -> FAIL, a clean one -> PASS) instead of a constant per-kind
    // score. Falls back to the display identifier if the argv isn't a published package.
    const scanTarget = fetchableCoordinateFromArgv(entry.argv) ?? identifier

    try {
      const scan = await scanInstall('mcp', scanTarget)
      if (!alive.current) return
      if (!scan || !['PASS','WARN','FAIL'].includes(scan.verdict)
        || typeof scan.scan_id !== 'string' || !scan.scan_id.trim()
        || typeof scan.requires_owner_approval !== 'boolean') throw new Error('unverified scan')
      // WARN and FAIL always route through the approval modal so the owner can
      // review and confirm the exact action — no silent toast degradation.
      if (scan.requires_owner_approval || scan.verdict === 'WARN' || scan.verdict === 'FAIL') {
        setPendingInstall({ scan, entry, collectedEnv, onDone })
        return
      }
      // PASS → proceed directly
      await doAddMcpServer(entry, collectedEnv, onDone)
    } catch {
      if (!alive.current) return
      show(t('skills.scan.unavailable'), 'error')
      onDone()
    }
  }

  async function handleScanApprove() {
    if (!pendingInstall) return
    const { scan, entry, collectedEnv, onDone } = pendingInstall
    setPendingInstall(null)
    try {
      const decision = await recordSecurityDecision({
        scan_id: scan.scan_id,
        decision: 'approve',
        identifier: scan.identifier ?? entry.server_id ?? entry.id ?? '',
        kind: 'mcp',
        score: scan.score,
        verdict: scan.verdict,
        risks_json: JSON.stringify(scan.risks),
        mcp_approval: {
          operation: 'add',
          server_id: entry.server_id ?? entry.id ?? slugify(entry.name ?? ''),
          label: entry.label ?? entry.name,
          argv: Array.isArray(entry.argv) ? entry.argv : String(entry.argv ?? '').split(/\s+/).filter(Boolean),
          env: { ...collectedEnv },
        },
      })
      await doAddMcpServer(entry, collectedEnv, onDone, true, decision.approval_grant)
    } catch (e) {
      show(e instanceof Error ? e.message : t('mcp.err.decision'), 'error')
      onDone()
    }
  }

  async function searchRegistry() {
    const q = regInputRef.current?.value.trim() ?? ''
    if (q.length < 2) return
    const request = ++searchGeneration.current
    setRegistryState({ status: 'loading' })
    try {
      const results = await searchMcpRegistry(q)
      if (request !== searchGeneration.current) return
      if (!Array.isArray(results)) throw new Error('invalid MCP search')
      const arr = results
      setRegistryState({ status: 'success', results: arr })
    } catch {
      if (request !== searchGeneration.current) return
      setRegistryState({
        status: 'error',
        message: t('mcp.err.registry_search'),
      })
    }
  }

  return (
    <>
      {ConfirmDialogNode}
      {pendingInstall && (
        <InstallScanModal
          scan={pendingInstall.scan}
          name={pendingInstall.entry.label ?? pendingInstall.entry.name ?? pendingInstall.scan.identifier ?? ''}
          onApprove={handleScanApprove}
          onCancel={() => {
            pendingInstall.onDone()
            setPendingInstall(null)
          }}
        />
      )}
      <PageHeader
        title={t('view.mcp')}
        subtitle={t('mcp.subtitle')}
      />

      <div className={`view-body cv-view-body ${styles.viewBody}`}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>

          {/* ── Active servers ──────────────────────────────────────────────── */}
          <div>
            <section className="cv-section" aria-label={t('mcp.active.aria')}>
              <h2 className={styles.sectionLabel}>{t('mcp.active')}</h2>

              {state.status === 'loading' && (
                <div
                  style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}
                  aria-busy="true"
                  aria-label={t('mcp.loading_aria')}
                >
                  {[...Array(2)].map((_, i) => (
                    <div key={i} className={styles.skeletonRow}>
                      <div
                        className="skeleton skeleton--avatar"
                        style={{ borderRadius: 'var(--radius-sm)', animationDelay: `${i * 80}ms` }}
                      />
                      <div className={styles.skeletonRowLines}>
                        <div
                          className="skeleton skeleton--line"
                          style={{ width: '40%', animationDelay: `${i * 80 + 30}ms` }}
                        />
                        <div
                          className="skeleton skeleton--line-sm"
                          style={{ width: '65%', animationDelay: `${i * 80 + 60}ms` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {state.status === 'error' && (
                <div>
                  <div role="alert" className={styles.errorBlock}>
                    <p className={styles.errorMessage}>{state.message}</p>
                    <div>
                      <Button variant="secondary" size="sm" onClick={load}>
                        {t('mcp.retry')}
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              {state.status === 'success' && (
                state.servers.length === 0
                  ? (
                    <EmptyState
                      compact
                      icon={<Wrench size={32} />}
                      title={t('mcp.empty.title')}
                      description={t('mcp.empty.desc')}
                      action={
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => {
                            document.getElementById('mcp-registry-input')?.focus()
                          }}
                        >
                          {t('mcp.empty.cta')}
                        </Button>
                      }
                    />
                  )
                  : (
                    <ul className="cv-list" role="list">
                      <AnimatePresence initial={false}>
                        {state.servers.map(s => (
                          <AnimatedListItem key={s.server_id ?? s.id}>
                            <McpServerRow
                              server={s}
                              onRemove={() => handleRemoveServer(s)}
                            />
                          </AnimatedListItem>
                        ))}
                      </AnimatePresence>
                    </ul>
                  )
              )}
            </section>
          </div>

          {/* ── Managed presets (Safent-operated MCP bridges) ────────────────── */}
          <div>
            <section className="cv-section" aria-label={t('mcp.managed.section.aria')}>
              <h2 className={styles.sectionLabel}>{t('mcp.managed.section')}</h2>
              <ul className="cv-list" role="list">
                <AnimatedListItem>
                  <ManagedRemotePresetCard
                    connectedServer={safentAdsServer}
                    onConnected={load}
                    onRemove={handleRemoveServer}
                  />
                </AnimatedListItem>
              </ul>
            </section>
          </div>

          {/* ── Suggested catalog ───────────────────────────────────────────── */}
          <div>
            <section className="cv-section" aria-label={t('mcp.suggested.aria')}>
              <h2 className={styles.sectionLabel}>{t('mcp.suggested')}</h2>
              <ul className="cv-list" role="list">
                <AnimatePresence initial={false}>
                  {MCP_CATALOG.map(entry => (
                    <AnimatedListItem key={entry.server_id}>
                      <CatalogCard
                        entry={entry}
                        installedIds={installedIds}
                        onInstall={installEntry}
                      />
                    </AnimatedListItem>
                  ))}
                </AnimatePresence>
              </ul>
            </section>
          </div>

          {/* ── Official registry search ─────────────────────────────────── */}
          <div>
            <section className="cv-section" aria-label={t('mcp.search.aria')}>
              <h2 className={styles.sectionLabel}>{t('mcp.search.title')}</h2>
              <div className={styles.searchBar}>
                <label className="sr-only" htmlFor="mcp-registry-input">
                  {t('mcp.search.label')}
                </label>
                <div className={styles.searchInputWrap}>
                  <span className={styles.searchIcon} aria-hidden="true">
                    <Search size={13} />
                  </span>
                  <input
                    id="mcp-registry-input"
                    ref={regInputRef}
                    className={styles.searchInput}
                    type="search"
                    placeholder="github, slack, postgres…"
                    autoComplete="off"
                    onKeyDown={e => { if (e.key === 'Enter') searchRegistry() }}
                  />
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={searchRegistry}
                  loading={registryState.status === 'loading'}
                >
                  {t('mcp.search.btn')}
                </Button>
              </div>
              <p className={styles.searchHint}>
                {t('mcp.search.hint')}
              </p>

              {registryState.status === 'error' && (
                <div>
                  <div role="alert" className={styles.errorBlock}>
                    <p className={styles.errorMessage}>{registryState.message}</p>
                    <div>
                      <Button variant="secondary" size="sm" onClick={searchRegistry}>
                        {t('mcp.retry')}
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              {registryState.status === 'success' && registryState.results.length > 0 && (
                <ul className="cv-list" role="list" style={{ marginTop: 'var(--space-3)' }}>
                  <AnimatePresence initial={false}>
                    {registryState.results.map((entry, i) => (
                      <AnimatedListItem key={`${entry.server_id ?? entry.id ?? entry.name ?? i}`}>
                        <CatalogCard
                          entry={entry}
                          installedIds={installedIds}
                          onInstall={installEntry}
                        />
                      </AnimatedListItem>
                    ))}
                  </AnimatePresence>
                </ul>
              )}

              {registryState.status === 'success' && registryState.results.length === 0 && (
                <EmptyState
                  icon={<Search size={28} />}
                  title={t('mcp.search.empty.title')}
                  description={t('mcp.search.empty.desc')}
                />
              )}
            </section>
          </div>

          {/* ── Manual add ──────────────────────────────────────────────────── */}
          <div>
            <section className="cv-section" aria-label={t('mcp.manual.aria')}>
              <h2 className={styles.sectionLabel}>{t('mcp.manual.aria')}</h2>
              <AddMcpForm
                onAdded={() => { show(t('mcp.toast.added_generic'), 'ok'); load() }}
                onToast={show}
              />
            </section>
          </div>

        </div>
      </div>
    </>
  )
}

// ── Active server row ─────────────────────────────────────────────────────────

interface McpServerRowProps {
  server: McpServer
  onRemove: () => void
}

function McpServerRow({ server, onRemove }: McpServerRowProps) {
  const t = useT()
  const [showCmd, setShowCmd] = useState(false)
  const argv = Array.isArray(server.argv) ? server.argv.join(' ') : (server.argv ?? '')
  const healthy = String(server.health ?? '').toLowerCase() === 'healthy'
  const hasHealth = server.health != null && server.health !== ''
  const toolCount = server.tool_count
  const toolLabel = toolCount != null
    ? (toolCount === 1 ? t('mcp.tool_count.one') : t('mcp.tool_count.many').replace('{n}', String(toolCount)))
    : ''
  const serverName = server.label ?? server.server_id ?? t('mcp.fallback_name')

  return (
    <HoverRow className={styles.serverRow}>
      <span className={styles.serverIcon} aria-hidden="true">
        <Terminal size={14} />
      </span>

      <div className={styles.serverInfo}>
        <div className={styles.serverName}>
          {serverName}

          {hasHealth && (
            <StatusDot
              state={healthy ? 'success' : 'danger'}
              label={toolLabel || String(server.health)}
            />
          )}
          {!hasHealth && toolLabel && (
            <span className={styles.toolCount}>{toolLabel}</span>
          )}
        </div>

        {argv && (
          <button
            type="button"
            className={styles.serverCmdToggle}
            onClick={() => setShowCmd(v => !v)}
            aria-expanded={showCmd}
            aria-label={showCmd ? t('mcp.details.hide') : t('mcp.details.show')}
          >
            <AnimatedChevron open={showCmd} size={10} />
            <span>{t('mcp.details.label')}</span>
          </button>
        )}

        <AnimatedExpanderContent open={showCmd && Boolean(argv)}>
          <code className={styles.serverCmdText}>{argv}</code>
        </AnimatedExpanderContent>
      </div>

      <div className={styles.serverActions}>
        <Button
          variant="danger"
          size="sm"
          onClick={onRemove}
          aria-label={t('mcp.remove.aria').replace('{name}', server.label ?? t('mcp.fallback_name_lower'))}
        >
          <X size={13} aria-hidden="true" />
        </Button>
      </div>
    </HoverRow>
  )
}

// ── Connected safent-ads preset: server row + quick links into its panel ────
//
// The ads service serves its own React panel at `/` on the SAME host as the
// MCP bridge (`/mcp`) — panelOriginFromMcpUrl strips the path, https only,
// never built from free text (the URL comes straight from the argv the
// backend already validated when it registered this connection).

// Seeded-companion status (024) -> {label i18n key, StatusDot state}. A
// status value this build doesn't recognise yet (future finer states from
// the companion's own /mcp/health) falls back to a generic "installed" dot
// rather than showing nothing.
const COMPANION_STATUS_META: Record<string, { key: TranslationKey; state: StatusDotState }> = {
  esperando_servicio: { key: 'mcp.managed.ads.status.waiting', state: 'warning' },
  listo: { key: 'mcp.managed.ads.status.ready', state: 'success' },
}

interface ConnectedAdsPresetProps {
  server: McpServer
  onRemove: () => void
}

function ConnectedAdsPreset({ server, onRemove }: ConnectedAdsPresetProps) {
  const t = useT()
  const argv = Array.isArray(server.argv) ? server.argv : []
  // The URL is always the element right after "mcp-remote", never assumed
  // to be the LAST one — a seeded companion's argv (hermes.shell_server.
  // companions.CompanionEndpoint.argv) appends "--header" "Authorization:
  // Bearer ${ADS_BEARER}" after the URL, so argv[argv.length - 1] would
  // read the header value instead of the URL for that slug.
  const mcpUrl = argv[argv.indexOf('mcp-remote') + 1] ?? ''
  const origin = mcpUrl ? panelOriginFromMcpUrl(mcpUrl) : null
  const companionMeta = server.companion_status
    ? (COMPANION_STATUS_META[server.companion_status]
        ?? { key: 'mcp.managed.ads.status.installed', state: 'success' as StatusDotState })
    : null

  return (
    <>
      <McpServerRow server={server} onRemove={onRemove} />
      {companionMeta && (
        <div className={styles.companionStatus}>
          <StatusDot state={companionMeta.state} label={t(companionMeta.key)} />
        </div>
      )}
      {origin && (
        <div className={styles.adsPanel}>
          <div className={styles.adsPanelHead}>
            <Megaphone size={13} aria-hidden="true" />
            <span>{t('ads.panel.title')}</span>
          </div>
          <div className={styles.adsPanelActions}>
            <a className="cv-btn cv-btn--secondary cv-btn--sm" href={origin} target="_blank" rel="noopener noreferrer">
              <ExternalLink size={13} aria-hidden="true" />
              {t('ads.panel.open')}
            </a>
            <a className="cv-btn cv-btn--secondary cv-btn--sm" href={`${origin}/conexiones`} target="_blank" rel="noopener noreferrer">
              <Link2 size={13} aria-hidden="true" />
              {t('ads.panel.connections')}
            </a>
            <a className="cv-btn cv-btn--secondary cv-btn--sm" href={`${origin}/propuestas`} target="_blank" rel="noopener noreferrer">
              <Lightbulb size={13} aria-hidden="true" />
              {t('ads.panel.proposals')}
            </a>
          </div>
          <p className={styles.adsPanelHint}>{t('ads.panel.hint')}</p>
        </div>
      )}
    </>
  )
}

// ── Safent Ads managed-remote preset card ───────────────────────────────────

interface ManagedRemotePresetCardProps {
  connectedServer: McpServer | undefined
  onConnected: () => void
  onRemove: (server: McpServer) => void
}

function ManagedRemotePresetCard({ connectedServer, onConnected, onRemove }: ManagedRemotePresetCardProps) {
  const t = useT()
  const { locale } = useLocale()
  const [url, setUrl] = useState('')
  const [connecting, setConnecting] = useState(false)
  const [pendingScan, setPendingScan] = useState<{ scan: InstallScanResponse; url: string } | null>(null)
  // 029: the self-host URL is an escape hatch now, collapsed and off by
  // default — CompanionInstallAction ("Instalar") is the default path.
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const availability = useAdsAvailability()

  useEffect(() => {
    if (connectedServer) return
    // Pre-fill from any URL the owner already set (e.g. via the D-Bus verb
    // directly, or a previous session) so re-visiting the page doesn't lose it.
    listManagedRemoteEndpoints()
      .then(res => {
        const saved = res.endpoints?.[SAFENT_ADS_SLUG]
        if (saved) setUrl(saved)
      })
      .catch(() => undefined)
  }, [connectedServer])

  async function doConnect(force: boolean, targetUrl = url.trim(), approvalGrant?: string) {
    setConnecting(true)
    try {
      const res = await connectManagedRemote(SAFENT_ADS_SLUG, targetUrl, force, approvalGrant)
      if (res && res.tool_count === 0) {
        show(t('mcp.toast.no_tools').replace('{name}', t('mcp.managed.ads.title')), 'warn', 7000)
      } else {
        show(t('mcp.managed.ads.toast.connected'), 'ok')
      }
      onConnected()
    } catch (e) {
      // The daemon's own install security-scan (embedded in add_mcp_server)
      // blocked this — its ad-hoc block dict isn't rich enough for the review
      // modal, so re-scan through the dedicated endpoint to get a full,
      // owner-reviewable verdict (same two-phase pattern as installEntry()).
      //
      // A blocked scan is now a 403 (mcp_api.py._raise_if_failed), so
      // request<T>'s !res.ok branch wraps the daemon's {ok, blocked, ...}
      // result under `detail` — read it from there, not the response top
      // level (which is just {detail: ...} now, never {blocked: ...} itself).
      const detail = e instanceof ApiError && e.body && typeof e.body === 'object'
        ? (e.body as Record<string, unknown>)['detail']
        : null
      const blocked = detail && typeof detail === 'object'
        ? (detail as Record<string, unknown>)['blocked']
        : null
      if (blocked === true) {
        try {
          const scan = await scanInstall('mcp', SAFENT_ADS_SCAN_TARGET)
          setPendingScan({ scan, url: targetUrl })
          return
        } catch {
          show(e instanceof Error ? e.message : t('mcp.err.generic'), 'error')
          return
        }
      }
      show(e instanceof Error ? e.message : t('mcp.err.generic'), 'error')
    } finally {
      setConnecting(false)
    }
  }

  function handleConnectClick() {
    const error = validateManagedRemoteUrl(t, url)
    if (error) {
      show(error, 'warn')
      return
    }
    void doConnect(false)
  }

  async function handleScanApprove() {
    if (!pendingScan) return
    const { scan, url: approvedUrl } = pendingScan
    setPendingScan(null)
    try {
      const decision = await recordSecurityDecision({
        scan_id: scan.scan_id,
        decision: 'approve',
        identifier: scan.identifier ?? SAFENT_ADS_SCAN_TARGET,
        kind: 'mcp',
        score: scan.score,
        verdict: scan.verdict,
        risks_json: JSON.stringify(scan.risks),
        mcp_approval: { operation: 'managed_remote', slug: SAFENT_ADS_SLUG, url: approvedUrl },
      })
      await doConnect(true, approvedUrl, decision.approval_grant)
    } catch (e) {
      show(e instanceof Error ? e.message : t('mcp.err.decision'), 'error')
    }
  }

  if (availability.status === 'managed') {
    return <div className={styles.catalogCard}>
      <div className={styles.catalogCardName}>Safent Ads · Enterprise</div>
      <p className={styles.catalogCardDesc}>{locale === 'es'
        ? 'Las asignaciones y la conexión se administran desde Enterprise. Consulta las cuentas disponibles en Anuncios.'
        : 'Assignments and the connection are managed by Enterprise. Open Ads to inspect available accounts.'}</p>
    </div>
  }

  if (connectedServer) {
    return <ConnectedAdsPreset server={connectedServer} onRemove={() => onRemove(connectedServer)} />
  }

  return (
    <>
      {pendingScan && (
        <InstallScanModal
          scan={pendingScan.scan}
          name={t('mcp.managed.ads.title')}
          onApprove={handleScanApprove}
          onCancel={() => setPendingScan(null)}
        />
      )}
      <motion.div className={styles.catalogCard} transition={SPRING} layout>
        <div className={styles.catalogCardMain}>
          <span className={styles.catalogCardIcon} aria-hidden="true">
            <Megaphone size={14} />
          </span>
          <div className={styles.catalogCardInfo}>
            <div className={styles.catalogCardName}>{t('mcp.managed.ads.title')}</div>
            <p className={styles.catalogCardDesc}>{t('mcp.managed.ads.desc')}</p>
          </div>
        </div>

        {/* Default path (029 FR-001/FR-002): one "Instalar" action, no URL, no
            connection field — the SAME flow the sidebar's not_installed entry
            triggers. Renders nothing while ready/loading (CompanionInstallAction
            owns that judgment via useAdsAvailability). */}
        <CompanionInstallAction availability={availability} />

        <button
          type="button"
          className={styles.serverCmdToggle}
          onClick={() => setAdvancedOpen(v => !v)}
          aria-expanded={advancedOpen}
          aria-label={advancedOpen ? t('mcp.managed.ads.advanced.hide') : t('mcp.managed.ads.advanced.show')}
        >
          <AnimatedChevron open={advancedOpen} size={10} />
          <span>{t('mcp.managed.ads.advanced.toggle')}</span>
        </button>

        <AnimatedExpanderContent open={advancedOpen}>
          <p className={styles.catalogCardDesc}>{t('mcp.managed.ads.advanced.hint')}</p>
          <div className={styles.envForm}>
            <div className={styles.envField}>
              <label className={styles.envLabel} htmlFor="mcp-managed-ads-url">
                {t('mcp.managed.ads.url.label')}
              </label>
              <input
                id="mcp-managed-ads-url"
                className={styles.envInput}
                type="url"
                inputMode="url"
                autoComplete="off"
                placeholder={t('mcp.managed.ads.url.placeholder')}
                value={url}
                onChange={e => setUrl(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleConnectClick() }}
              />
            </div>
            <div className={styles.envActions}>
              <Button
                variant="primary"
                size="sm"
                type="button"
                loading={connecting}
                disabled={connecting}
                onClick={handleConnectClick}
              >
                {connecting ? t('mcp.managed.connecting') : t('mcp.managed.connect')}
              </Button>
            </div>
          </div>
        </AnimatedExpanderContent>
      </motion.div>
    </>
  )
}

// ── Catalog / registry card ───────────────────────────────────────────────────

interface CatalogCardProps {
  entry: McpRegistryEntry
  installedIds: Set<string>
  onInstall: (entry: McpRegistryEntry, env: Record<string, string>, onDone: () => void) => void
}

function CatalogCard({ entry, installedIds, onInstall }: CatalogCardProps) {
  const t = useT()
  const [installing, setInstalling] = useState(false)
  const [showEnvForm, setShowEnvForm] = useState(false)
  const [envValues, setEnvValues] = useState<Record<string, string>>({})
  const id = entry.server_id ?? entry.id ?? slugify(entry.name ?? '')
  const already = installedIds.has(id) || installedIds.has(entry.server_id ?? '')
  const runner = getRunner(entry.argv)
  // Compatible = stdio transport via npx (npm) or uvx (pypi), not explicitly disabled.
  // Everything else — remote/SSE/OCI/Docker/unknown argv — is unsupported in this container.
  const stdioCompatible = (runner === 'npx' || runner === 'uvx') && entry.installable !== false
  const unsupported = !stdioCompatible
  const envSchema = parseEnvSchema(entry)
  const needsEnv = envSchema.length > 0
  const repo = entry.repository ?? entry.homepage ?? entry.website ?? ''

  function handleInstallClick() {
    if (needsEnv) {
      setShowEnvForm(true)
    } else {
      setInstalling(true)
      onInstall(entry, {}, () => setInstalling(false))
    }
  }

  function handleEnvSubmit() {
    // Validate required fields
    for (const field of envSchema) {
      if (field.required && !(envValues[field.key] ?? '').trim()) {
        show(t('mcp.env.required').replace('{field}', field.label), 'warn')
        return
      }
    }
    setShowEnvForm(false)
    setInstalling(true)
    onInstall(entry, { ...envValues }, () => setInstalling(false))
  }

  return (
    <motion.div
      className={styles.catalogCard}
      transition={SPRING}
      layout
    >
      {/* Main row */}
      <div className={styles.catalogCardMain}>
        <span className={styles.catalogCardIcon} aria-hidden="true">
          <Terminal size={14} />
        </span>

        <div className={styles.catalogCardInfo}>
          <div className={styles.catalogCardName}>
            {entry.label ?? entry.name ?? id}
            {entry.tag && (
              <DsBadge variant="default">{entry.tag}</DsBadge>
            )}
            {needsEnv && (
              <DsBadge variant="warning">{t('mcp.badge.requires_key')}</DsBadge>
            )}
            {already && (
              <DsBadge variant="success">{t('mcp.badge.added')}</DsBadge>
            )}
          </div>

          {entry.description && (
            <p className={styles.catalogCardDesc}>{entry.description}</p>
          )}

          {unsupported && (
            <p className={styles.catalogCardWarn}>
              {entry.unsupported_reason ?? (
                runner === ''
                  ? t('mcp.unsupported.remote_only')
                  : t('mcp.unsupported.runner').replace('{runner}', runner)
              )}
            </p>
          )}
        </div>

        <div className={styles.catalogCardActions}>
          {repo && (
            <a
              href={repo}
              target="_blank"
              rel="noopener noreferrer"
              className={styles.docsLink}
              aria-label={t('mcp.docs.aria').replace('{name}', entry.label ?? entry.name ?? id)}
            >
              <ExternalLink size={11} aria-hidden="true" style={{ marginRight: 4 }} />
              {t('mcp.docs')}
            </a>
          )}
          {!showEnvForm && (
            <Button
              variant={already ? 'ghost' : 'secondary'}
              size="sm"
              disabled={already || unsupported}
              loading={installing}
              onClick={handleInstallClick}
            >
              {already ? t('mcp.badge.added') : unsupported ? t('mcp.unavailable') : t('mcp.add')}
            </Button>
          )}
        </div>
      </div>

      {/* Inline key-entry form */}
      <AnimatedExpanderContent open={showEnvForm}>
        <div className={styles.envForm}>
          {envSchema.map(field => (
            <div key={field.key} className={styles.envField}>
              <label className={styles.envLabel} htmlFor={`mcp-env-${id}-${field.key}`}>
                {field.label}{field.required ? ' *' : ''}
              </label>
              <input
                id={`mcp-env-${id}-${field.key}`}
                className={styles.envInput}
                type={field.secret ? 'password' : 'text'}
                autoComplete="off"
                value={envValues[field.key] ?? ''}
                onChange={e => setEnvValues(prev => ({ ...prev, [field.key]: e.target.value }))}
              />
            </div>
          ))}
          <div className={styles.envActions}>
            <Button variant="primary" size="sm" type="button" onClick={handleEnvSubmit}>
              {t('mcp.add')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              type="button"
              onClick={() => { setShowEnvForm(false); setEnvValues({}) }}
            >
              {t('mcp.cancel')}
            </Button>
          </div>
        </div>
      </AnimatedExpanderContent>
    </motion.div>
  )
}

// ── Manual add form ───────────────────────────────────────────────────────────

interface AddMcpFormProps {
  onAdded: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function AddMcpForm({ onAdded, onToast }: AddMcpFormProps) {
  const t = useT()
  const [adding, setAdding] = useState(false)
  const labelRef = useRef<HTMLInputElement>(null)
  const argvRef = useRef<HTMLInputElement>(null)
  const envRef = useRef<HTMLTextAreaElement>(null)

  async function handleAdd() {
    const label = labelRef.current?.value.trim() ?? ''
    const argvRaw = argvRef.current?.value.trim() ?? ''
    if (!label || !argvRaw) {
      onToast(t('mcp.form.err.required'), 'warn')
      return
    }

    const argv = argvRaw.split(/\s+/).filter(Boolean)
    const envRaw = envRef.current?.value.trim() ?? ''
    const env: Record<string, string> = {}
    envRaw.split('\n').forEach(line => {
      const idx = line.indexOf('=')
      if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
    })

    setAdding(true)
    try {
      const res = await addMcpServer({
        server_id: label.toLowerCase().replace(/\s+/g, '_'),
        label,
        argv,
        env,
      })
      const name = label
      if (res && res.tool_count === 0) {
        onToast(t('mcp.toast.no_tools').replace('{name}', name), 'warn')
      } else {
        onToast(t('mcp.toast.added_generic'), 'ok')
      }
      if (labelRef.current) labelRef.current.value = ''
      if (argvRef.current) argvRef.current.value = ''
      if (envRef.current) envRef.current.value = ''
      onAdded()
    } catch (e) {
      onToast(e instanceof Error ? e.message : t('mcp.err.generic'), 'error')
    } finally { setAdding(false) }
  }

  return (
    <motion.div
      className={styles.addForm}
      transition={TWEEN_FAST}
      layout
    >
      <h3 className={styles.addFormTitle}>{t('mcp.form.title')}</h3>

      <div className={styles.addFormField}>
        <label className={styles.addFormLabel} htmlFor="mcp-label">
          {t('mcp.form.name')}
        </label>
        <input
          id="mcp-label"
          ref={labelRef}
          className={styles.addFormInput}
          type="text"
          placeholder="Replicate, Brave…"
          autoComplete="off"
        />
      </div>

      <div className={styles.addFormField}>
        <label className={styles.addFormLabel} htmlFor="mcp-argv">
          {t('mcp.form.command')}
        </label>
        <input
          id="mcp-argv"
          ref={argvRef}
          className={`${styles.addFormInput} ${styles.addFormInputMono}`}
          type="text"
          placeholder="npx -y @modelcontextprotocol/server-brave-search"
          autoComplete="off"
        />
      </div>

      <div className={styles.addFormField}>
        <label className={styles.addFormLabel} htmlFor="mcp-env">
          {t('mcp.env.label')}
        </label>
        <textarea
          id="mcp-env"
          ref={envRef}
          className={styles.addFormTextarea}
          rows={3}
          placeholder="BRAVE_API_KEY=br-xxx"
        />
      </div>

      <div className={styles.addFormActions}>
        <Button
          variant="primary"
          size="sm"
          onClick={handleAdd}
          loading={adding}
          disabled={adding}
        >
          {t('mcp.add')}
        </Button>
      </div>
    </motion.div>
  )
}
