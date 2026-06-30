import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { X, Terminal, Search, Wrench, ExternalLink } from 'lucide-react'
import { useT } from '../lib/i18n'
import { listMcpServers, addMcpServer, removeMcpServer, searchMcpRegistry, scanInstall, recordSecurityDecision, ApiError } from '../api/client'
import type { McpServer, McpRegistryEntry, InstallScanResponse } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'
import InstallScanModal from '../components/InstallScanModal'
import type { MfaFactors } from '../components/MfaModal'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import { Badge as DsBadge, StatusDot } from '../components/ui/Badge'
import {
  AnimatePresence,
  AnimatedListItem,
  AnimatedExpanderContent,
  AnimatedChevron,
  FadeIn,
  Stagger,
  StaggerItem,
  HoverRow,
  motion,
  SPRING,
  TWEEN_FAST,
} from '../components/ui/motion'
import styles from './McpView.module.css'

// Curated catalog of verified one-click MCP servers (npx/uvx).
const MCP_CATALOG: McpRegistryEntry[] = [
  {
    server_id: 'github',
    label: 'GitHub',
    tag: 'Dev',
    description: 'Acceso a tus repositorios, issues y pull requests de GitHub.',
    argv: ['npx', '-y', '@modelcontextprotocol/server-github'],
    repository: 'https://github.com/github/github-mcp-server',
  },
  {
    server_id: 'context7',
    label: 'Context7',
    tag: 'Docs',
    description: 'Documentación de librerías en vivo, siempre actualizada.',
    argv: ['npx', '-y', '@upstash/context7-mcp'],
    repository: 'https://github.com/upstash/context7',
  },
  {
    server_id: 'filesystem',
    label: 'Archivos locales',
    tag: 'Sistema',
    description: 'Lee y escribe ficheros locales. Cada acción requiere tu permiso.',
    argv: ['npx', '-y', '@modelcontextprotocol/server-filesystem', '/var/lib/hermes/workspace'],
    repository: 'https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem',
  },
]

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
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })
  const [registryState, setRegistryState] = useState<RegistryState>({ status: 'idle' })
  const [pendingInstall, setPendingInstall] = useState<PendingInstall | null>(null)
  const regInputRef = useRef<HTMLInputElement>(null)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  function load() {
    dispatch({ type: 'LOADING' })
    listMcpServers()
      // Ruflo is a first-class Lumen integration, not a user-managed tool set.
      // The backend already hides it but we filter defensively client-side too.
      .then(servers => dispatch({ type: 'LOADED', servers: servers.filter(s => s.slug !== 'ruflo') }))
      .catch((e: unknown) => dispatch({
        type: 'FAILED',
        message: e instanceof ApiError ? e.message : 'No se pudieron cargar las herramientas externas.',
      }))
  }

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const installedIds = state.status === 'success'
    ? new Set(state.servers.map(s => s.server_id ?? s.id ?? ''))
    : new Set<string>()

  async function doAddMcpServer(entry: McpRegistryEntry, collectedEnv: Record<string, string>, onDone: () => void, force = false) {
    const argv = Array.isArray(entry.argv)
      ? entry.argv
      : String(entry.argv ?? '').split(/\s+/).filter(Boolean)

    try {
      const res = await addMcpServer({
        server_id: entry.server_id ?? entry.id ?? slugify(entry.name ?? ''),
        label: entry.label ?? entry.name,
        argv,
        env: { ...collectedEnv },
        // Owner sovereign override after a FAIL/WARN scan was approved with MFA — the
        // daemon's add gate re-blocks FAIL/WARN unless force carries the approval.
        force,
      })
      const name = entry.label ?? entry.name ?? ''
      if (res && res.tool_count === 0) {
        show(`"${name}" se conectó pero no tiene herramientas disponibles. Revisa su configuración.`, 'warn', 7000)
      } else {
        show(`"${name}" añadida — tus agentes ya pueden usarla`, 'ok')
      }
      load()
    } catch (e) {
      show(e instanceof Error ? e.message : 'Error', 'error')
    } finally {
      onDone()
    }
  }

  async function installEntry(entry: McpRegistryEntry, collectedEnv: Record<string, string>, onDone: () => void) {
    // npx (npm) and uvx (PyPI) both resolve to a published package the content +
    // CVE scanners can fetch and statically analyze, so the verdict is REAL. Other
    // runners (local node/python3 scripts, inline commands) have no inspectable
    // coordinate → kept out of the one-click path; the backend scan also treats an
    // MCP with no published package as non-analyzable (owner review, never PASS).
    const runner = getRunner(entry.argv)
    const ALLOWED_RUNNERS = ['npx', 'uvx']
    if (runner && !ALLOWED_RUNNERS.includes(runner)) {
      show(`Por ahora se admiten herramientas npx y uvx (esta usa ${runner}).`, 'warn', 7000)
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
      // WARN and FAIL always route through the approval modal so the owner can
      // review and confirm with TOTP — no silent toast degradation.
      if (scan.requires_owner_approval || scan.verdict === 'WARN' || scan.verdict === 'FAIL') {
        setPendingInstall({ scan, entry, collectedEnv, onDone })
        return
      }
      // PASS → proceed directly
      await doAddMcpServer(entry, collectedEnv, onDone)
    } catch {
      // Scan endpoint unavailable — fall back to direct install
      await doAddMcpServer(entry, collectedEnv, onDone)
    }
  }

  async function handleScanApprove(factors: MfaFactors) {
    if (!pendingInstall) return
    const { scan, entry, collectedEnv, onDone } = pendingInstall
    setPendingInstall(null)
    try {
      await recordSecurityDecision({
        scan_id: scan.scan_id,
        decision: 'approve',
        identifier: scan.identifier ?? entry.server_id ?? entry.id ?? '',
        kind: 'mcp',
        score: scan.score,
        verdict: scan.verdict,
        risks_json: JSON.stringify(scan.risks),
        totp: factors.totp,
      })
      await doAddMcpServer(entry, collectedEnv, onDone, true)
    } catch (e) {
      show(e instanceof Error ? e.message : 'Error al registrar la decisión', 'error')
      onDone()
    }
  }

  async function searchRegistry() {
    const q = regInputRef.current?.value.trim() ?? ''
    if (q.length < 2) return
    setRegistryState({ status: 'loading' })
    try {
      const results = await searchMcpRegistry(q)
      const arr = Array.isArray(results) ? results : []
      setRegistryState({ status: 'success', results: arr })
    } catch (e) {
      setRegistryState({
        status: 'error',
        message: e instanceof ApiError ? e.message : 'No se pudo buscar en el registro.',
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
        title="Herramientas externas"
        subtitle="Conecta conjuntos de herramientas externos para ampliar las capacidades del agente."
      />

      <div className="view-body cv-view-body">
        <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>

          {/* ── Active servers ──────────────────────────────────────────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Herramientas activas">
              <h2 className={styles.sectionLabel}>Activas</h2>

              {state.status === 'loading' && (
                <div
                  style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}
                  aria-busy="true"
                  aria-label="Cargando herramientas…"
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
                <FadeIn>
                  <div role="alert" className={styles.errorBlock}>
                    <p className={styles.errorMessage}>{state.message}</p>
                    <div>
                      <Button variant="secondary" size="sm" onClick={load}>
                        Reintentar
                      </Button>
                    </div>
                  </div>
                </FadeIn>
              )}

              {state.status === 'success' && (
                state.servers.length === 0
                  ? (
                    <EmptyState
                      compact
                      icon={<Wrench size={32} />}
                      title="Sin herramientas conectadas"
                      description="Añade una del catálogo sugerido o busca en el registro para ampliar las capacidades del agente."
                      action={
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => {
                            document.getElementById('mcp-registry-input')?.focus()
                          }}
                        >
                          Buscar herramientas
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
                              onRemove={async () => {
                                const name = s.label ?? s.server_id ?? ''
                                const ok = await confirm({
                                  title: `¿Eliminar "${name}"?`,
                                  description: 'El agente dejará de tener acceso a estas herramientas.',
                                  confirmLabel: 'Eliminar',
                                  variant: 'danger',
                                })
                                if (!ok) return
                                try {
                                  await removeMcpServer(s.server_id ?? s.id ?? '')
                                  show('Conjunto de herramientas eliminado', 'ok')
                                  load()
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
              )}
            </section>
          </StaggerItem>

          {/* ── Suggested catalog ───────────────────────────────────────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Herramientas sugeridas">
              <h2 className={styles.sectionLabel}>Sugeridas</h2>
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
          </StaggerItem>

          {/* ── Official registry search ─────────────────────────────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Buscar más herramientas">
              <h2 className={styles.sectionLabel}>Buscar más herramientas</h2>
              <div className={styles.searchBar}>
                <label className="sr-only" htmlFor="mcp-registry-input">
                  Buscar herramientas externas
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
                  Buscar
                </Button>
              </div>
              <p className={styles.searchHint}>
                Conectado al registro oficial de herramientas externas
              </p>

              {registryState.status === 'error' && (
                <FadeIn>
                  <div role="alert" className={styles.errorBlock}>
                    <p className={styles.errorMessage}>{registryState.message}</p>
                    <div>
                      <Button variant="secondary" size="sm" onClick={searchRegistry}>
                        Reintentar
                      </Button>
                    </div>
                  </div>
                </FadeIn>
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
                  title="Sin resultados"
                  description="Prueba con otro término de búsqueda."
                />
              )}
            </section>
          </StaggerItem>

          {/* ── Manual add ──────────────────────────────────────────────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Añadir manualmente">
              <h2 className={styles.sectionLabel}>Añadir manualmente</h2>
              <AddMcpForm
                onAdded={() => { show('Herramienta añadida — tus agentes ya pueden usarla', 'ok'); load() }}
                onToast={show}
              />
            </section>
          </StaggerItem>

        </Stagger>
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
  const [showCmd, setShowCmd] = useState(false)
  const argv = Array.isArray(server.argv) ? server.argv.join(' ') : (server.argv ?? '')
  const healthy = String(server.health ?? '').toLowerCase() === 'healthy'
  const hasHealth = server.health != null && server.health !== ''
  const toolCount = server.tool_count
  const toolLabel = toolCount != null
    ? `${toolCount} herramienta${toolCount === 1 ? '' : 's'}`
    : ''

  return (
    <HoverRow className={styles.serverRow}>
      <span className={styles.serverIcon} aria-hidden="true">
        <Terminal size={14} />
      </span>

      <div className={styles.serverInfo}>
        <div className={styles.serverName}>
          {server.label ?? server.server_id ?? 'Herramienta externa'}

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
            aria-label={showCmd ? 'Ocultar detalles técnicos' : 'Ver detalles técnicos'}
          >
            <AnimatedChevron open={showCmd} size={10} />
            <span>Detalles técnicos</span>
          </button>
        )}

        <AnimatedExpanderContent open={showCmd && Boolean(argv)}>
          <code className={styles.serverCmdText}>{argv}</code>
        </AnimatedExpanderContent>
      </div>

      <div className={styles.serverActions}>
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
          onClick={onRemove}
          aria-label={`Eliminar ${server.label ?? 'herramienta externa'}`}
        >
          <X size={13} aria-hidden="true" />
        </button>
      </div>
    </HoverRow>
  )
}

// ── Catalog / registry card ───────────────────────────────────────────────────

interface CatalogCardProps {
  entry: McpRegistryEntry
  installedIds: Set<string>
  onInstall: (entry: McpRegistryEntry, env: Record<string, string>, onDone: () => void) => void
}

function CatalogCard({ entry, installedIds, onInstall }: CatalogCardProps) {
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
        show(`"${field.label}" es obligatorio`, 'warn')
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
      whileHover={{ y: -1 }}
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
              <DsBadge variant="warning">Requiere clave API</DsBadge>
            )}
            {already && (
              <DsBadge variant="success">Añadida</DsBadge>
            )}
          </div>

          {entry.description && (
            <p className={styles.catalogCardDesc}>{entry.description}</p>
          )}

          {unsupported && (
            <p className={styles.catalogCardWarn}>
              {entry.unsupported_reason ?? (
                runner === ''
                  ? 'Solo remoto — sin paquete stdio npm/pypi.'
                  : `Solo se admiten herramientas npx/uvx (esta usa ${runner}).`
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
              aria-label={`Documentación de ${entry.label ?? entry.name ?? id} (se abre en nueva pestaña)`}
            >
              <ExternalLink size={11} aria-hidden="true" style={{ marginRight: 4 }} />
              Docs
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
              {already ? 'Añadida' : unsupported ? 'No disponible' : 'Añadir'}
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
              Añadir
            </Button>
            <Button
              variant="ghost"
              size="sm"
              type="button"
              onClick={() => { setShowEnvForm(false); setEnvValues({}) }}
            >
              Cancelar
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
      onToast('Nombre y comando de arranque son obligatorios', 'warn')
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
        onToast(`"${name}" se conectó pero no tiene herramientas disponibles. Revisa su configuración.`, 'warn')
      } else {
        onToast('Herramienta añadida — tus agentes ya pueden usarla', 'ok')
      }
      if (labelRef.current) labelRef.current.value = ''
      if (argvRef.current) argvRef.current.value = ''
      if (envRef.current) envRef.current.value = ''
      onAdded()
    } catch (e) {
      onToast(e instanceof Error ? e.message : 'Error', 'error')
    } finally { setAdding(false) }
  }

  return (
    <motion.div
      className={styles.addForm}
      whileHover={{ y: -1 }}
      transition={TWEEN_FAST}
      layout
    >
      <h3 className={styles.addFormTitle}>Añadir herramienta externa</h3>

      <div className={styles.addFormField}>
        <label className={styles.addFormLabel} htmlFor="mcp-label">
          Nombre
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
          Comando de arranque
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
          Añadir
        </Button>
      </div>
    </motion.div>
  )
}
