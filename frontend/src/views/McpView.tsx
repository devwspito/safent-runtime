import { useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { listMcpServers, addMcpServer, removeMcpServer, searchMcpRegistry, ApiError } from '../api/client'
import type { McpServer, McpRegistryEntry } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'

// Curated catalog — mirrors mcp.js MCP_CATALOG (npx-only verified servers).
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

export default function McpView() {
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })
  const [registryState, setRegistryState] = useState<RegistryState>({ status: 'idle' })
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

  async function installEntry(entry: McpRegistryEntry, collectedEnv: Record<string, string>, onDone: () => void) {
    const runner = getRunner(entry.argv)
    if (runner && runner !== 'npx') {
      show(`Solo se admiten herramientas npx por ahora (esta usa ${runner}).`, 'warn', 7000)
      onDone()
      return
    }

    const argv = Array.isArray(entry.argv)
      ? entry.argv
      : String(entry.argv ?? '').split(/\s+/).filter(Boolean)

    try {
      const res = await addMcpServer({
        server_id: entry.server_id ?? entry.id ?? slugify(entry.name ?? ''),
        label: entry.label ?? entry.name,
        argv,
        env: { ...collectedEnv },
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
      <header className="view-header">
        <h1 className="view-title">Herramientas externas</h1>
        <p className="view-subtitle">Conecta conjuntos de herramientas externos para ampliar las capacidades del agente.</p>
      </header>

      <div className="view-body cv-view-body">
        {/* ── Active servers ──────────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Herramientas activas">
          <h2 className="cv-section-label">Activas</h2>
          {state.status === 'loading' && <div className="cv-skeleton" aria-busy="true" />}
          {state.status === 'error' && (
            <div role="alert">
              <p className="state-error">{state.message}</p>
              <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={load} style={{ marginTop: 8 }}>Reintentar</button>
            </div>
          )}
          {state.status === 'success' && (
            state.servers.length === 0
              ? <p className="cv-empty">Sin herramientas conectadas. Añade una.</p>
              : (
                <ul className="cv-list" role="list">
                  {state.servers.map(s => (
                    <li key={s.server_id ?? s.id}>
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
                    </li>
                  ))}
                </ul>
              )
          )}
        </section>

        {/* ── Suggested catalog ───────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Herramientas sugeridas">
          <h2 className="cv-section-label">Sugeridas</h2>
          <div className="mcp-cards-grid">
            {MCP_CATALOG.map(entry => (
              <CatalogCard
                key={entry.server_id}
                entry={entry}
                installedIds={installedIds}
                onInstall={installEntry}
              />
            ))}
          </div>
        </section>

        {/* ── Official registry search ─────────────────────────────────── */}
        <section className="cv-section" aria-label="Buscar más herramientas">
          <h2 className="cv-section-label">Buscar más herramientas</h2>
          <div className="cv-search-row">
            <label className="sr-only" htmlFor="mcp-registry-input">Buscar herramientas externas</label>
            <input
              id="mcp-registry-input"
              ref={regInputRef}
              className="cv-input"
              type="search"
              placeholder="Buscar (github, slack, postgres…)"
              autoComplete="off"
              onKeyDown={e => { if (e.key === 'Enter') searchRegistry() }}
            />
            <button
              className="cv-btn cv-btn--secondary cv-btn--sm"
              onClick={searchRegistry}
              disabled={registryState.status === 'loading'}
            >
              {registryState.status === 'loading' ? 'Buscando…' : 'Buscar'}
            </button>
          </div>
          <p className="cv-hint">Conectado al registro oficial de herramientas externas</p>
          {registryState.status === 'error' && (
            <div role="alert">
              <p className="state-error">{registryState.message}</p>
              <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={searchRegistry} style={{ marginTop: 8 }}>
                Reintentar
              </button>
            </div>
          )}
          {registryState.status === 'success' && registryState.results.length > 0 && (
            <div className="mcp-cards-grid">
              {registryState.results.map((entry, i) => (
                <CatalogCard
                  key={`${entry.server_id ?? entry.id ?? entry.name ?? i}`}
                  entry={entry}
                  installedIds={installedIds}
                  onInstall={installEntry}
                />
              ))}
            </div>
          )}
          {registryState.status === 'success' && registryState.results.length === 0 && (
            <p className="cv-empty">Sin resultados.</p>
          )}
        </section>

        {/* ── Manual add ──────────────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Añadir manualmente">
          <h2 className="cv-section-label">Añadir manualmente</h2>
          <AddMcpForm onAdded={() => { show('Herramienta añadida — tus agentes ya pueden usarla', 'ok'); load() }} onToast={show} />
        </section>
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
  const argv = Array.isArray(server.argv) ? server.argv.join(' ') : (server.argv ?? '')
  const healthy = String(server.health ?? '').toLowerCase() === 'healthy'
  const hasHealth = server.health != null && server.health !== ''
  const tools = server.tool_count != null ? `${server.tool_count} herramienta${server.tool_count === 1 ? '' : 's'}` : ''

  return (
    <div className="mcp-row">
      <div className="mcp-row__info">
        <div className="mcp-row__name">
          {server.label ?? server.server_id ?? 'Herramienta externa'}
          {hasHealth && (
            <span className={`mcp-health-chip${healthy ? ' is-ok' : ' is-down'}`}>
              {healthy ? '●' : '○'} {tools || String(server.health)}
            </span>
          )}
          {!hasHealth && tools && <span className="mcp-health-chip">{tools}</span>}
        </div>
        {/* Show the launch command under a technical details toggle */}
        {argv && (
          <details style={{ marginTop: 2 }}>
            <summary className="mcp-row__cmd" style={{ cursor: 'pointer', listStyle: 'none', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <span style={{ fontSize: 10, opacity: 0.5 }}>▶</span>
              <span style={{ fontSize: 'var(--text-caption)', opacity: 0.6 }}>Ver detalles técnicos</span>
            </summary>
            <div className="mcp-row__cmd" style={{ marginTop: 4 }}>{argv}</div>
          </details>
        )}
      </div>
      <button
        className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
        onClick={onRemove}
        aria-label={`Eliminar ${server.label ?? 'herramienta externa'}`}
      >
        ✕
      </button>
    </div>
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
  const nonNpx = runner !== '' && runner !== 'npx'
  const unsupported = entry.installable === false || nonNpx
  const argv = Array.isArray(entry.argv) ? entry.argv.join(' ') : (entry.argv ?? '')
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
    <div className="mcp-card">
      <div className="mcp-card__info">
        <div className="mcp-card__head">
          <span className="mcp-card__name">{entry.label ?? entry.name ?? id}</span>
          {entry.tag && <span className="mcp-card__tag">{entry.tag}</span>}
          {needsEnv && <span className="mcp-card__tag">Requiere tu clave API</span>}
        </div>
        {entry.description && <div className="mcp-card__desc">{entry.description}</div>}
        {/* Technical details collapsed by default */}
        {argv && (
          <details style={{ marginTop: 4 }}>
            <summary style={{ cursor: 'pointer', listStyle: 'none', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <span style={{ fontSize: 10, opacity: 0.5 }}>▶</span>
              <span className="mcp-card__cmd" style={{ opacity: 0.6 }}>Ver detalles técnicos</span>
            </summary>
            <div className="mcp-card__cmd" style={{ marginTop: 2 }}>{argv}</div>
          </details>
        )}
        {unsupported && entry.unsupported_reason && (
          <div className="mcp-card__cmd">{entry.unsupported_reason}</div>
        )}
        {unsupported && nonNpx && !entry.unsupported_reason && (
          <div className="mcp-card__cmd">Solo se admiten herramientas npx por ahora (esta usa {runner}).</div>
        )}
      </div>

      {/* Inline key-entry form — shown when the entry requires configuration */}
      {showEnvForm && (
        <div className="cv-form-stack" style={{ marginTop: 'var(--sp-3)' }}>
          {envSchema.map(field => (
            <div key={field.key}>
              <label className="cv-label" htmlFor={`mcp-env-${id}-${field.key}`}>
                {field.label}{field.required ? ' *' : ''}
              </label>
              <input
                id={`mcp-env-${id}-${field.key}`}
                className="cv-input"
                type={field.secret ? 'password' : 'text'}
                autoComplete="off"
                value={envValues[field.key] ?? ''}
                onChange={e => setEnvValues(prev => ({ ...prev, [field.key]: e.target.value }))}
              />
            </div>
          ))}
          <div className="cv-form-actions">
            <button className="cv-btn cv-btn--primary cv-btn--sm" type="button" onClick={handleEnvSubmit}>
              Añadir
            </button>
            <button
              className="cv-btn cv-btn--ghost cv-btn--sm"
              type="button"
              onClick={() => { setShowEnvForm(false); setEnvValues({}) }}
            >
              Cancelar
            </button>
          </div>
        </div>
      )}

      <div className="mcp-card__actions">
        {repo && (
          <a
            href={repo}
            target="_blank"
            rel="noopener noreferrer"
            className="cv-link cv-btn--sm"
          >
            Docs
          </a>
        )}
        {!showEnvForm && (
          <button
            className="cv-btn cv-btn--secondary cv-btn--sm"
            disabled={already || unsupported || installing}
            onClick={handleInstallClick}
          >
            {already ? 'Añadida' : unsupported ? 'No disponible' : installing ? 'Añadiendo…' : 'Añadir'}
          </button>
        )}
      </div>
    </div>
  )
}

// ── Manual add form ───────────────────────────────────────────────────────────

interface AddMcpFormProps {
  onAdded: () => void
  onToast: (msg: string, kind: 'ok' | 'warn' | 'error') => void
}

function AddMcpForm({ onAdded, onToast }: AddMcpFormProps) {
  const [adding, setAdding] = useState(false)
  const labelRef = useRef<HTMLInputElement>(null)
  const argvRef = useRef<HTMLInputElement>(null)
  const envRef = useRef<HTMLTextAreaElement>(null)

  async function handleAdd() {
    const label = labelRef.current?.value.trim() ?? ''
    const argvRaw = argvRef.current?.value.trim() ?? ''
    if (!label || !argvRaw) { onToast('Nombre y comando de arranque son obligatorios', 'warn'); return }

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
    <div className="cv-form-card">
      <h3 className="cv-form-title">Añadir herramienta externa</h3>
      <label className="cv-label" htmlFor="mcp-label">Nombre</label>
      <input
        id="mcp-label"
        ref={labelRef}
        className="cv-input"
        type="text"
        placeholder="Replicate, Brave…"
        autoComplete="off"
      />
      <label className="cv-label" htmlFor="mcp-argv">Comando de arranque</label>
      <input
        id="mcp-argv"
        ref={argvRef}
        className="cv-input"
        type="text"
        placeholder="npx -y @modelcontextprotocol/server-brave-search"
        autoComplete="off"
      />
      <label className="cv-label" htmlFor="mcp-env">Variables de configuración (CLAVE=VALOR, una por línea)</label>
      <textarea
        id="mcp-env"
        ref={envRef}
        className="cv-textarea"
        rows={3}
        placeholder="BRAVE_API_KEY=br-xxx"
      />
      <div className="cv-form-actions">
        <button
          className="cv-btn cv-btn--primary cv-btn--sm"
          onClick={handleAdd}
          disabled={adding}
        >
          {adding ? 'Añadiendo…' : 'Añadir'}
        </button>
      </div>
    </div>
  )
}
