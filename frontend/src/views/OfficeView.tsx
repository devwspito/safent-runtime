import { lazy, Suspense, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { listAgents, getRuntimeStatus, listMcpServers, createAgent } from '../api/client'
import type { Agent, RuntimeStatus, CreateAgentPayload } from '../api/types'

// ── Lazy-load the canvas so the rAF loop only starts when En-vivo is shown ──

const OfficeCanvas = lazy(() =>
  import('./office-live/OfficeCanvas').then((m) => ({ default: m.OfficeCanvas }))
)

// ── View-level state ─────────────────────────────────────────────────────────

type Tab = 'tarjetas' | 'live'

type DataState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; agents: Agent[]; runtimeStatus: RuntimeStatus; hasRuflo: boolean }

type DataAction =
  | { type: 'LOADED'; agents: Agent[]; runtimeStatus: RuntimeStatus; hasRuflo: boolean }
  | { type: 'FAILED'; message: string }
  | { type: 'STATUS_UPDATE'; runtimeStatus: RuntimeStatus }

function dataReducer(state: DataState, action: DataAction): DataState {
  switch (action.type) {
    case 'LOADED':
      return { status: 'ready', agents: action.agents, runtimeStatus: action.runtimeStatus, hasRuflo: action.hasRuflo }
    case 'FAILED':
      return { status: 'error', message: action.message }
    case 'STATUS_UPDATE':
      if (state.status !== 'ready') return state
      return { ...state, runtimeStatus: action.runtimeStatus }
  }
}

// ── CreateAgentModal ──────────────────────────────────────────────────────────

interface CreateAgentModalProps {
  onClose: () => void
  onCreated: (agent: Agent) => void
}

function CreateAgentModal({ onClose, onCreated }: CreateAgentModalProps) {
  const [name, setName] = useState('')
  const [role, setRole] = useState('')
  const [mission, setMission] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameId = 'create-agent-name'
  const roleId = 'create-agent-role'
  const missionId = 'create-agent-mission'
  const errorId = 'create-agent-error'
  const firstInputRef = useRef<HTMLInputElement>(null)

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
        role: role.trim() || undefined,
        primary_mission: mission.trim() || undefined,
      }
      const created = await createAgent(payload)
      onCreated(created)
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
          <h2 id="create-agent-title" className="office-modal-title">Nuevo agente</h2>
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
            <label htmlFor={roleId} className="office-field-label">Rol</label>
            <input
              id={roleId}
              type="text"
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="office-field-input"
              maxLength={80}
              placeholder="Ej: Especialista en soporte"
            />
          </div>

          <div className="office-field">
            <label htmlFor={missionId} className="office-field-label">Misión</label>
            <textarea
              id={missionId}
              value={mission}
              onChange={(e) => setMission(e.target.value)}
              className="office-field-input office-field-textarea"
              maxLength={500}
              rows={3}
              placeholder="Describe la tarea principal del agente…"
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
              {pending ? 'Creando…' : 'Crear agente'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ── AgentDrawer ───────────────────────────────────────────────────────────────

interface AgentDrawerProps {
  agent: Agent
  isWorking: boolean
  onClose: () => void
}

function AgentDrawer({ agent, isWorking, onClose }: AgentDrawerProps) {
  const navigate = useNavigate()
  const initials = agent.name.charAt(0).toUpperCase()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

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
          <div className="agent-avatar" style={{ background: agent.color }} aria-hidden="true">
            {initials}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 id="agent-drawer-title" className="office-drawer-title">{agent.name}</h2>
            {agent.role && <p className="agent-role" style={{ margin: 0 }}>{agent.role}</p>}
          </div>
          {agent.is_default && <span className="badge">Cerebro</span>}
          <button type="button" className="office-modal-close" onClick={onClose} aria-label="Cerrar">✕</button>
        </div>

        <div className="office-drawer-body">
          {agent.primary_mission && (
            <p className="agent-mission" style={{ marginBottom: 'var(--sp-5)' }}>
              {agent.primary_mission}
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
              onClick={() => { navigate('/chat'); onClose() }}
            >
              Chatear
            </button>
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
          </div>
        </div>
      </div>
    </div>
  )
}

// ── AgentCard (Tarjetas view) ─────────────────────────────────────────────────

interface AgentCardProps {
  agent: Agent
  isWorking: boolean
  onClick: () => void
}

function AgentCard({ agent, isWorking, onClick }: AgentCardProps) {
  const initials = agent.name.charAt(0).toUpperCase()

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
        <div className="agent-avatar" style={{ background: agent.color }} aria-hidden="true">
          {initials}
        </div>
        <div className="agent-meta">
          <p className="agent-name">{agent.name}</p>
          {agent.role && <p className="agent-role">{agent.role}</p>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', flexShrink: 0 }}>
          {agent.is_default && <span className="badge">Cerebro</span>}
          <span
            className="office-status-dot"
            style={{ background: isWorking ? 'var(--warn)' : 'var(--ok)' }}
            aria-label={isWorking ? 'Trabajando' : 'En línea'}
            role="img"
          />
        </div>
      </div>
      {agent.primary_mission && <p className="agent-mission">{agent.primary_mission}</p>}
    </article>
  )
}

// ── TarjetasView ──────────────────────────────────────────────────────────────

interface TarjetasViewProps {
  agents: Agent[]
  runtimeStatus: RuntimeStatus
  hasRuflo: boolean
  onAgentsChange: (agents: Agent[]) => void
}

function TarjetasView({ agents, runtimeStatus, hasRuflo, onAgentsChange }: TarjetasViewProps) {
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null)

  const activeIds = new Set<string>()
  if (runtimeStatus.active_agent_id) activeIds.add(runtimeStatus.active_agent_id)
  for (const a of runtimeStatus.activity ?? []) activeIds.add(a.agent_id)

  const cerebro = agents.filter((a) => a.is_default)
  const misAgentes = agents.filter((a) => !a.is_default)

  const handleAgentCreated = (created: Agent) => {
    onAgentsChange([...agents, created])
    setShowCreateModal(false)
  }

  return (
    <>
      <div className="office-tarjetas">
        {/* ── Cerebro ── */}
        {cerebro.length > 0 && (
          <section aria-labelledby="section-cerebro" className="office-section">
            <h2 id="section-cerebro" className="office-section-title">Cerebro</h2>
            <p className="office-section-desc">Orquestador principal — coordina todos los agentes.</p>
            <ul className="agent-grid" role="list">
              {cerebro.map((a) => (
                <li key={a.id}>
                  <AgentCard
                    agent={a}
                    isWorking={activeIds.has(a.id)}
                    onClick={() => setSelectedAgent(a)}
                  />
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* ── Mis agentes ── */}
        <section aria-labelledby="section-agentes" className="office-section">
          <h2 id="section-agentes" className="office-section-title">Mis agentes</h2>
          {misAgentes.length === 0 ? (
            <p className="state-label" style={{ padding: 0 }}>No tienes agentes adicionales.</p>
          ) : (
            <ul className="agent-grid" role="list">
              {misAgentes.map((a) => (
                <li key={a.id}>
                  <AgentCard
                    agent={a}
                    isWorking={activeIds.has(a.id)}
                    onClick={() => setSelectedAgent(a)}
                  />
                </li>
              ))}
              <li>
                <button
                  type="button"
                  className="agent-card office-create-card"
                  onClick={() => setShowCreateModal(true)}
                  aria-label="Crear nuevo agente"
                >
                  <span className="office-create-icon" aria-hidden="true">+</span>
                  <span className="office-create-label">Crear agente</span>
                </button>
              </li>
            </ul>
          )}
          {misAgentes.length === 0 && (
            <button
              type="button"
              className="office-btn office-btn--ghost"
              style={{ marginTop: 'var(--sp-4)' }}
              onClick={() => setShowCreateModal(true)}
            >
              + Crear agente
            </button>
          )}
        </section>

        {/* ── Swarm ruflo ── */}
        {hasRuflo && (
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
      </div>

      {showCreateModal && (
        <CreateAgentModal
          onClose={() => setShowCreateModal(false)}
          onCreated={handleAgentCreated}
        />
      )}

      {selectedAgent && (
        <AgentDrawer
          agent={selectedAgent}
          isWorking={activeIds.has(selectedAgent.id)}
          onClose={() => setSelectedAgent(null)}
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

  // ── Initial load ─────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const [agents, runtimeStatus, mcpServers] = await Promise.all([
          listAgents(),
          getRuntimeStatus(),
          listMcpServers(),
        ])
        if (!cancelled) {
          const hasRuflo = mcpServers.some((s) => s.slug === 'ruflo')
          dispatch({ type: 'LOADED', agents, runtimeStatus, hasRuflo })
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : 'No se pudo cargar la oficina.'
          dispatch({ type: 'FAILED', message })
        }
      }
    }

    void load()
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

  const handleAgentClick = useCallback((agentId: string, _agentName: string) => {
    // Navigate to chat scoped to this agent when clicked in the live view
    navigate(`/chat?agent_id=${encodeURIComponent(agentId)}`)
  }, [navigate])

  return (
    <div className="office-view">
      {/* ── Header with segmented toggle ── */}
      <header className="view-header office-view-header">
        <div className="office-header-row">
          <div>
            <h1 className="view-title">Office</h1>
            <p className="view-subtitle">El piso en tiempo real de tus agentes</p>
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
                agents={state.agents}
                runtimeStatus={state.runtimeStatus}
                hasRuflo={state.hasRuflo}
                onAgentsChange={(agents) =>
                  dispatch({ type: 'LOADED', agents, runtimeStatus: state.runtimeStatus, hasRuflo: state.hasRuflo })
                }
              />
            )}

            {tab === 'live' && (
              <div className="office-live-container">
                <Suspense fallback={
                  <div className="state-container">
                    <p className="state-label">Cargando mapa…</p>
                  </div>
                }>
                  <OfficeCanvas
                    agents={state.agents}
                    runtimeStatus={state.runtimeStatus}
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
