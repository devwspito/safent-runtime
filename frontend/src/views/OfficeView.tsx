import { lazy, Suspense, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'

import { getAgentRoster, getRuntimeStatus, listMcpServers, createAgent, setActiveAgent } from '../api/client'
import type { AgentRoster, RosterAgent, RosterDepartment, RuntimeStatus, CreateAgentPayload } from '../api/types'
import type { LumenAgent, LumenRuntimeStatus } from './office-live/engine/office-state'

// ── Lazy-load the canvas so the rAF loop only starts when En-vivo is shown ──

const OfficeCanvas = lazy(() =>
  import('./office-live/OfficeCanvas').then((m) => ({ default: m.OfficeCanvas }))
)

// ── Map roster → engine types ─────────────────────────────────────────────────

/**
 * Converts a RosterAgent + its parent RosterDepartment into the LumenAgent
 * shape expected by the office engine. The department info is required so the
 * engine can build one room per department from the real roster structure.
 */
function rosterAgentToLumenAgent(a: RosterAgent, dept: RosterDepartment): LumenAgent {
  return {
    id: a.id,
    name: a.name,
    role: a.description,
    primary_mission: a.description,
    color: a.color ?? '#0A84FF',
    is_default: a.is_default,
    autonomy_level: 'balanced',
    department_id: dept.id,
    department_kind: dept.kind,
    department_name: dept.name,
  }
}

// ── View-level state ──────────────────────────────────────────────────────────

type Tab = 'tarjetas' | 'live'

type DataState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; roster: AgentRoster; runtimeStatus: RuntimeStatus; hasRuflo: boolean }

type DataAction =
  | { type: 'LOADED'; roster: AgentRoster; runtimeStatus: RuntimeStatus; hasRuflo: boolean }
  | { type: 'FAILED'; message: string }
  | { type: 'STATUS_UPDATE'; runtimeStatus: RuntimeStatus }

function dataReducer(state: DataState, action: DataAction): DataState {
  switch (action.type) {
    case 'LOADED':
      return { status: 'ready', roster: action.roster, runtimeStatus: action.runtimeStatus, hasRuflo: action.hasRuflo }
    case 'FAILED':
      return { status: 'error', message: action.message }
    case 'STATUS_UPDATE':
      if (state.status !== 'ready') return state
      return { ...state, runtimeStatus: action.runtimeStatus }
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function activeAgentIds(runtimeStatus: RuntimeStatus): Set<string> {
  const ids = new Set<string>()
  if (runtimeStatus.active_agent_id) ids.add(runtimeStatus.active_agent_id)
  for (const a of runtimeStatus.activity ?? []) ids.add(a.agent_id)
  return ids
}

// ── Department selector (used by create/edit modal) ───────────────────────────

interface DeptSelectorProps {
  departments: RosterDepartment[]
  value: string
  onChange: (v: string) => void
  id: string
}

function DeptSelector({ departments, value, onChange, id }: DeptSelectorProps) {
  // showCustom tracks whether the user explicitly chose "Nuevo departamento…"
  // We never mirror value back into a separate local state — the parent owns it.
  const [showCustom, setShowCustom] = useState(false)
  const customRef = useRef<HTMLInputElement>(null)

  const existingNames = departments.map((d) => d.name)

  // If the current value isn't in the list it must be a custom entry
  const isCustom = value !== '' && !existingNames.includes(value)
  const showInput = showCustom || isCustom

  function handleSelectChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const v = e.target.value
    if (v === '__new__') {
      setShowCustom(true)
      onChange('')
      setTimeout(() => customRef.current?.focus(), 0)
    } else {
      setShowCustom(false)
      onChange(v)
    }
  }

  function handleCustomChange(e: React.ChangeEvent<HTMLInputElement>) {
    onChange(e.target.value)
  }

  function handleClear() {
    setShowCustom(false)
    onChange('')
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-1)' }}>
      {!showInput ? (
        <select
          id={id}
          className="office-field-input"
          value={value}
          onChange={handleSelectChange}
        >
          <option value="">Sin departamento</option>
          {existingNames.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
          <option value="__new__">Nuevo departamento…</option>
        </select>
      ) : (
        <div style={{ display: 'flex', gap: 'var(--sp-2)' }}>
          <input
            ref={customRef}
            id={id}
            type="text"
            className="office-field-input"
            value={value}
            onChange={handleCustomChange}
            placeholder="Nombre del nuevo departamento"
            maxLength={60}
            style={{ flex: 1 }}
          />
          <button
            type="button"
            className="office-btn office-btn--ghost"
            style={{ height: 36, padding: '0 var(--sp-3)', fontSize: 'var(--text-label)' }}
            onClick={handleClear}
          >
            ✕
          </button>
        </div>
      )}
    </div>
  )
}

// ── CreateAgentModal ──────────────────────────────────────────────────────────

interface CreateAgentModalProps {
  departments: RosterDepartment[]
  prefill?: { name: string; description: string; department: string }
  onClose: () => void
  onCreated: (agent: RosterAgent) => void
}

function CreateAgentModal({ departments, prefill, onClose, onCreated }: CreateAgentModalProps) {
  const [name, setName] = useState(prefill?.name ?? '')
  const [description, setDescription] = useState(prefill?.description ?? '')
  const [department, setDepartment] = useState(prefill?.department ?? '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameId = 'create-agent-name'
  const descId = 'create-agent-desc'
  const deptId = 'create-agent-dept'
  const errorId = 'create-agent-error'
  const firstInputRef = useRef<HTMLInputElement>(null)
  const isClone = prefill !== undefined

  useEffect(() => {
    firstInputRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) { setError('El nombre es obligatorio.'); return }
    setPending(true)
    setError(null)
    try {
      const payload: CreateAgentPayload = {
        name: name.trim(),
        primary_mission: description.trim() || undefined,
        department: department.trim() || undefined,
      }
      const created = await createAgent(payload)
      // Map the Agent response to a RosterAgent for immediate UI update
      const rosterAgent: RosterAgent = {
        id: created.id,
        name: created.name,
        description: created.primary_mission ?? '',
        source: 'custom',
        department: department.trim() || '',
        is_default: created.is_default,
        color: created.color ?? null,
      }
      onCreated(rosterAgent)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error al crear el agente.')
    } finally {
      setPending(false)
    }
  }

  return (
    <div
      className="office-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-agent-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="office-modal">
        <div className="office-modal-header">
          <h2 id="create-agent-title" className="office-modal-title">
            {isClone ? 'Clonar agente' : 'Nuevo agente'}
          </h2>
          <button
            type="button"
            className="office-modal-close"
            onClick={onClose}
            aria-label="Cerrar"
          >
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate className="office-modal-form">
          {isClone && (
            <p style={{ fontSize: 'var(--text-label)', color: 'var(--ink3)', marginTop: '-var(--sp-2)' }}>
              Copia personalizable de un agente Ruflo. Puedes modificarla libremente.
            </p>
          )}

          <div className="office-field">
            <label htmlFor={nameId} className="office-field-label">Nombre *</label>
            <input
              ref={firstInputRef}
              id={nameId}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="office-field-input"
              required
              aria-required="true"
              aria-describedby={error ? errorId : undefined}
              maxLength={80}
              placeholder="Ej: Asistente ventas"
            />
          </div>

          <div className="office-field">
            <label htmlFor={descId} className="office-field-label">Descripción</label>
            <textarea
              id={descId}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="office-field-input office-field-textarea"
              maxLength={500}
              rows={3}
              placeholder="Describe la tarea principal del agente…"
            />
          </div>

          <div className="office-field">
            <label htmlFor={deptId} className="office-field-label">Departamento</label>
            <DeptSelector
              id={deptId}
              departments={departments}
              value={department}
              onChange={setDepartment}
            />
          </div>

          {error && (
            <p id={errorId} role="alert" className="office-field-error">
              {error}
            </p>
          )}

          <div className="office-modal-actions">
            <button
              type="button"
              onClick={onClose}
              className="office-btn office-btn--ghost"
              disabled={pending}
            >
              Cancelar
            </button>
            <button
              type="submit"
              className="office-btn office-btn--primary"
              disabled={pending}
              aria-busy={pending}
            >
              {pending
                ? (isClone ? 'Clonando…' : 'Creando…')
                : (isClone ? 'Crear copia' : 'Crear agente')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ── AgentDrawer ───────────────────────────────────────────────────────────────

interface AgentDrawerProps {
  agent: RosterAgent
  isWorking: boolean
  onClose: () => void
  onClone: (agent: RosterAgent) => void
}

function AgentDrawer({ agent, isWorking, onClose, onClone }: AgentDrawerProps) {
  const navigate = useNavigate()
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'
  const [activating, setActivating] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function handleChat() {
    // Activate the agent if it's not the default, then navigate to /chat
    if (!agent.is_default) {
      setActivating(true)
      try {
        await setActiveAgent(agent.id)
        sileo.success({ title: `${agent.name} ahora está activo` })
      } catch {
        sileo.warning({ title: `No se pudo activar ${agent.name}` })
      } finally {
        setActivating(false)
      }
    }
    navigate('/chat')
    onClose()
  }

  // Strip the "custom:<slug>" namespace the backend uses when displaying the dept label
  const deptLabel = agent.department
    ? agent.department.replace(/^custom:/i, '')
    : ''

  return (
    <div
      className="office-drawer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="agent-drawer-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="office-drawer">
        <div className="office-drawer-header">
          <div
            className="agent-avatar"
            style={{ background: agent.color ?? 'var(--accent)' }}
            aria-hidden="true"
          >
            {initials}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 id="agent-drawer-title" className="office-drawer-title">{agent.name}</h2>
            {deptLabel && (
              <p className="agent-role" style={{ margin: 0 }}>{deptLabel}</p>
            )}
          </div>
          {agent.is_default && <span className="badge">Cerebro</span>}
          {isFactory && (
            <span
              className="badge"
              style={{
                background: 'color-mix(in srgb, var(--ok) 15%, transparent)',
                color: 'var(--ok)',
              }}
            >
              Ruflo
            </span>
          )}
          <button type="button" className="office-modal-close" onClick={onClose} aria-label="Cerrar">✕</button>
        </div>

        <div className="office-drawer-body">
          {agent.description && (
            <p className="agent-mission" style={{ marginBottom: 'var(--sp-5)' }}>
              {agent.description}
            </p>
          )}

          <div className="office-drawer-status">
            <span
              className="office-status-dot"
              style={{ background: isWorking ? 'var(--warn)' : 'var(--ok)' }}
              aria-hidden="true"
            />
            <span style={{ fontSize: 'var(--text-label)', color: 'var(--ink3)' }}>
              {isWorking ? 'Trabajando' : 'En línea'}
            </span>
          </div>

          <div className="office-drawer-actions">
            <button
              type="button"
              className="office-btn office-btn--primary"
              onClick={handleChat}
              disabled={activating}
              aria-busy={activating}
            >
              {activating ? 'Activando…' : 'Chatear'}
            </button>

            {isFactory ? (
              <button
                type="button"
                className="office-btn office-btn--ghost"
                onClick={() => { onClone(agent); onClose() }}
              >
                Clonar y personalizar
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="office-btn office-btn--ghost"
                  onClick={() => { navigate('/programadas'); onClose() }}
                >
                  Tarea
                </button>
                <button
                  type="button"
                  className="office-btn office-btn--ghost"
                  onClick={() => { navigate('/agentes'); onClose() }}
                >
                  Gestionar
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── AgentCard (Tarjetas view) ─────────────────────────────────────────────────

interface AgentCardProps {
  agent: RosterAgent
  isWorking: boolean
  onClick: () => void
}

function AgentCard({ agent, isWorking, onClick }: AgentCardProps) {
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'

  return (
    <article
      className="agent-card office-agent-card"
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}
      aria-label={`${agent.name}${isWorking ? ', trabajando' : ''}. Click para ver detalle.`}
      style={{ cursor: 'pointer' }}
    >
      <div className="agent-card-header">
        <div
          className="agent-avatar"
          style={{ background: agent.color ?? 'var(--accent)' }}
          aria-hidden="true"
        >
          {initials}
        </div>
        <div className="agent-meta">
          <p className="agent-name">{agent.name}</p>
          {agent.department && <p className="agent-role">{agent.department}</p>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', flexShrink: 0 }}>
          {agent.is_default && <span className="badge">Cerebro</span>}
          {isFactory && (
            <span
              className="badge"
              style={{
                background: 'color-mix(in srgb, var(--ok) 15%, transparent)',
                color: 'var(--ok)',
                fontSize: 'var(--text-micro)',
              }}
            >
              Ruflo
            </span>
          )}
          <span
            className="office-status-dot"
            style={{ background: isWorking ? 'var(--warn)' : 'var(--ok)' }}
            aria-label={isWorking ? 'Trabajando' : 'En línea'}
            role="img"
          />
        </div>
      </div>
      {agent.description && <p className="agent-mission">{agent.description}</p>}
    </article>
  )
}

// ── DepartmentSection ─────────────────────────────────────────────────────────

interface DepartmentSectionProps {
  dept: RosterDepartment
  activeIds: Set<string>
  onAgentClick: (agent: RosterAgent) => void
  onCreateClick: () => void
  sectionIndex: number
}

function DepartmentSection({ dept, activeIds, onAgentClick, onCreateClick, sectionIndex }: DepartmentSectionProps) {
  const headingId = `section-dept-${dept.id}`
  const isCustomDept = dept.kind === 'custom'

  const descriptionByKind: Record<string, string> = {
    cerebro: 'Orquestador principal — coordina todos los agentes.',
    factory: 'Agentes especializados del swarm Ruflo — solo lectura.',
    custom: '',
  }

  return (
    <section aria-labelledby={headingId} className="office-section">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--sp-3)' }}>
        <h2 id={headingId} className="office-section-title">{dept.name}</h2>
        {dept.kind === 'factory' && (
          <span style={{ fontSize: 'var(--text-caption)', color: 'var(--ink4)' }}>Ruflo</span>
        )}
      </div>
      {descriptionByKind[dept.kind] && (
        <p className="office-section-desc">{descriptionByKind[dept.kind]}</p>
      )}

      <ul className="agent-grid" role="list">
        {dept.agents.map((a) => (
          <li key={a.id}>
            <AgentCard
              agent={a}
              isWorking={activeIds.has(a.id)}
              onClick={() => onAgentClick(a)}
            />
          </li>
        ))}
        {/* Only show the create card in the first custom department section, or
            as the last item when there are no custom sections yet */}
        {isCustomDept && sectionIndex === 0 && (
          <li>
            <button
              type="button"
              className="agent-card office-create-card"
              onClick={onCreateClick}
              aria-label="Crear nuevo agente"
            >
              <span className="office-create-icon" aria-hidden="true">+</span>
              <span className="office-create-label">Crear agente</span>
            </button>
          </li>
        )}
      </ul>
    </section>
  )
}

// ── TarjetasView ──────────────────────────────────────────────────────────────

interface TarjetasViewProps {
  roster: AgentRoster
  runtimeStatus: RuntimeStatus
  hasRuflo: boolean
  onRosterChange: (roster: AgentRoster) => void
  onRosterRefetch: () => void
}

interface ClonePrefill {
  name: string
  description: string
  department: string
}

function TarjetasView({ roster, runtimeStatus, hasRuflo, onRosterRefetch }: TarjetasViewProps) {
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [clonePrefill, setClonePrefill] = useState<ClonePrefill | undefined>(undefined)
  const [selectedAgent, setSelectedAgent] = useState<RosterAgent | null>(null)

  const activeIds = activeAgentIds(runtimeStatus)

  const cerebroDepts = roster.departments.filter((d) => d.kind === 'cerebro')
  const factoryDepts = roster.departments.filter((d) => d.kind === 'factory')
  const customDepts = roster.departments.filter((d) => d.kind === 'custom')
  const hasCustomDepts = customDepts.length > 0

  function handleAgentCreated(_agent: RosterAgent) {
    // Re-fetch the canonical roster from the server — avoids stale ids and
    // the custom:<slug> namespace the backend now uses.
    setShowCreateModal(false)
    setClonePrefill(undefined)
    onRosterRefetch()
  }

  function handleCloneRequest(agent: RosterAgent) {
    setClonePrefill({
      name: `${agent.name} (copia)`,
      description: agent.description,
      department: 'Mis agentes',
    })
    setShowCreateModal(true)
  }

  return (
    <>
      <div className="office-tarjetas">
        {/* ── Cerebro departments ── */}
        {cerebroDepts.map((dept) => (
          <DepartmentSection
            key={dept.id}
            dept={dept}
            activeIds={activeIds}
            onAgentClick={setSelectedAgent}
            onCreateClick={() => setShowCreateModal(true)}
            sectionIndex={0}
          />
        ))}

        {/* ── Custom (user) departments ── */}
        {customDepts.map((dept, i) => (
          <DepartmentSection
            key={dept.id}
            dept={dept}
            activeIds={activeIds}
            onAgentClick={setSelectedAgent}
            onCreateClick={() => setShowCreateModal(true)}
            sectionIndex={i}
          />
        ))}

        {/* ── Empty-state for custom agents when no custom departments exist ── */}
        {!hasCustomDepts && (
          <section aria-labelledby="section-mis-agentes" className="office-section">
            <h2 id="section-mis-agentes" className="office-section-title">Mis agentes</h2>
            <p className="state-label" style={{ padding: 0 }}>No tienes agentes personalizados aún.</p>
            <button
              type="button"
              className="office-btn office-btn--ghost"
              style={{ marginTop: 'var(--sp-4)', alignSelf: 'flex-start' }}
              onClick={() => setShowCreateModal(true)}
            >
              + Crear agente
            </button>
          </section>
        )}

        {/* ── Factory (Ruflo) departments ── */}
        {factoryDepts.map((dept) => (
          <DepartmentSection
            key={dept.id}
            dept={dept}
            activeIds={activeIds}
            onAgentClick={setSelectedAgent}
            onCreateClick={() => setShowCreateModal(true)}
            sectionIndex={0}
          />
        ))}

        {/* ── Ruflo swarm indicator (when detected via MCP but roster doesn't show it) ── */}
        {hasRuflo && factoryDepts.length === 0 && (
          <section aria-labelledby="section-ruflo" className="office-section">
            <h2 id="section-ruflo" className="office-section-title">Swarm Ruflo</h2>
            <p className="office-section-desc">
              Ruflo conectado — el agente puede invocar herramientas del swarm en tiempo real.
              {runtimeStatus.ruflo_active && (
                <span
                  className="office-status-dot"
                  style={{ background: 'var(--ok)', marginLeft: 8, verticalAlign: 'middle' }}
                  aria-label="Activo"
                  role="img"
                />
              )}
            </p>
          </section>
        )}

        {/* ── Powered by Ruflo ── */}
        <p style={{
          fontSize: 'var(--text-micro)',
          color: 'var(--ink4)',
          textAlign: 'center',
          paddingTop: 'var(--sp-4)',
          paddingBottom: 'var(--sp-2)',
        }}>
          Powered by Ruflo
        </p>
      </div>

      {showCreateModal && (
        <CreateAgentModal
          departments={roster.departments}
          prefill={clonePrefill}
          onClose={() => {
            setShowCreateModal(false)
            setClonePrefill(undefined)
          }}
          onCreated={handleAgentCreated}
        />
      )}

      {selectedAgent && (
        <AgentDrawer
          agent={selectedAgent}
          isWorking={activeIds.has(selectedAgent.id)}
          onClose={() => setSelectedAgent(null)}
          onClone={(agent) => {
            setSelectedAgent(null)
            handleCloneRequest(agent)
          }}
        />
      )}
    </>
  )
}

// ── OfficeView (root) ─────────────────────────────────────────────────────────

export default function OfficeView() {
  const [tab, setTab] = useState<Tab>('live')
  const [state, dispatch] = useReducer(dataReducer, { status: 'loading' })
  const navigate = useNavigate()
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Initial load + programmatic refetch ──────────────────────────────────
  const load = useCallback(async () => {
    try {
      const [roster, runtimeStatus, mcpServers] = await Promise.all([
        getAgentRoster(),
        getRuntimeStatus(),
        listMcpServers(),
      ])
      const hasRuflo = mcpServers.some((s) => s.slug === 'ruflo')
      dispatch({ type: 'LOADED', roster, runtimeStatus, hasRuflo })
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'No se pudo cargar la oficina.'
      dispatch({ type: 'FAILED', message })
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const [roster, runtimeStatus, mcpServers] = await Promise.all([
          getAgentRoster(),
          getRuntimeStatus(),
          listMcpServers(),
        ])
        if (!cancelled) {
          const hasRuflo = mcpServers.some((s) => s.slug === 'ruflo')
          dispatch({ type: 'LOADED', roster, runtimeStatus, hasRuflo })
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : 'No se pudo cargar la oficina.'
          dispatch({ type: 'FAILED', message })
        }
      }
    }
    void run()
    return () => { cancelled = true }
  }, [])

  // ── Runtime status polling (4 s) ────────────────────────────────────────
  useEffect(() => {
    pollRef.current = setInterval(async () => {
      try {
        const runtimeStatus = await getRuntimeStatus()
        dispatch({ type: 'STATUS_UPDATE', runtimeStatus })
      } catch { /* silent — stale status on network hiccup is acceptable */ }
    }, 4_000)

    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const handleAgentClick = useCallback(async (agentId: string, agentName: string) => {
    // Activate the clicked agent, then navigate to /chat
    const agent = state.status === 'ready'
      ? state.roster.departments.flatMap(d => d.agents).find(a => a.id === agentId)
      : undefined
    if (agent && !agent.is_default) {
      try {
        await setActiveAgent(agentId)
        sileo.success({ title: `${agentName} ahora está activo` })
      } catch {
        sileo.warning({ title: `No se pudo activar ${agentName}` })
      }
    }
    navigate('/chat')
  }, [navigate, state])

  // Fullscreen for the live floor — many users want the office maximised.
  const liveRef = useRef<HTMLDivElement>(null)
  const toggleFullscreen = useCallback(() => {
    const el = liveRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen?.()
  }, [])

  // Convert roster agents to the engine's LumenAgent shape for the canvas.
  // Each agent carries its department info so the engine builds one room per department.
  const engineAgents: LumenAgent[] = state.status === 'ready'
    ? state.roster.departments.flatMap((dept) =>
        dept.agents.map((a) => rosterAgentToLumenAgent(a, dept))
      )
    : []

  const totalAgentCount = engineAgents.length

  const engineRuntimeStatus: LumenRuntimeStatus = state.status === 'ready'
    ? state.runtimeStatus
    : { state: 'idle', active_task_count: 0 }

  return (
    <div className="office-view">
      {/* ── Header with segmented toggle ── */}
      <header className="view-header office-view-header">
        <div className="office-header-row">
          <div>
            <h1 className="view-title">Agentes</h1>
            <p className="view-subtitle">
              {state.status === 'ready'
                ? `Tu equipo de ${totalAgentCount} agente${totalAgentCount !== 1 ? 's' : ''}`
                : 'Tu equipo de IA'}
              {' — tarjetas o piso en vivo'}
            </p>
          </div>

          <div className="office-seg-toggle" role="group" aria-label="Vista de la oficina">
            <button
              type="button"
              className={`office-seg-btn${tab === 'tarjetas' ? ' office-seg-btn--active' : ''}`}
              onClick={() => setTab('tarjetas')}
              aria-pressed={tab === 'tarjetas'}
            >
              Tarjetas
            </button>
            <button
              type="button"
              className={`office-seg-btn${tab === 'live' ? ' office-seg-btn--active' : ''}`}
              onClick={() => setTab('live')}
              aria-pressed={tab === 'live'}
            >
              En vivo
            </button>
          </div>
        </div>
      </header>

      {/* ── Body ── */}
      <div className="office-body">
        {state.status === 'loading' && (
          <div className="state-container" aria-live="polite" aria-busy="true">
            <p className="state-label">Cargando la oficina…</p>
          </div>
        )}

        {state.status === 'error' && (
          <div className="state-container" role="alert">
            <p className="state-error">{state.message}</p>
          </div>
        )}

        {state.status === 'ready' && (
          <>
            {tab === 'tarjetas' && (
              <TarjetasView
                roster={state.roster}
                runtimeStatus={state.runtimeStatus}
                hasRuflo={state.hasRuflo}
                onRosterChange={(roster) =>
                  dispatch({
                    type: 'LOADED',
                    roster,
                    runtimeStatus: state.runtimeStatus,
                    hasRuflo: state.hasRuflo,
                  })
                }
                onRosterRefetch={load}
              />
            )}

            {tab === 'live' && (
              <div className="office-live-container" ref={liveRef}>
                <button
                  type="button"
                  className="office-fullscreen-btn"
                  onClick={toggleFullscreen}
                  aria-label="Pantalla completa"
                  title="Pantalla completa"
                >⛶</button>
                <Suspense fallback={
                  <div className="state-container">
                    <p className="state-label">Cargando mapa…</p>
                  </div>
                }>
                  <OfficeCanvas
                    agents={engineAgents}
                    runtimeStatus={engineRuntimeStatus}
                    onAgentClick={handleAgentClick}
                  />
                </Suspense>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
