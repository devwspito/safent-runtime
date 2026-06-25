import { lazy, Suspense, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'
import { X } from 'lucide-react'

import { getAgentRoster, getRuntimeStatus, listMcpServers, createAgent, setActiveAgent, updateAgent, deleteAgent } from '../api/client'
import type { AgentRoster, RosterAgent, RosterDepartment, RuntimeStatus, CreateAgentPayload, UpdateAgentPayload } from '../api/types'
import type { LumenAgent, LumenRuntimeStatus } from './office-live/engine/office-state'
import { useConfirmDialog } from '../components/ConfirmDialog'

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
            aria-label="Borrar búsqueda"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
      )}
    </div>
  )
}

// ── AgentFormModal — used for create, clone, and edit ─────────────────────────

type AgentFormMode = 'create' | 'clone' | 'edit'

interface AgentFormModalProps {
  departments: RosterDepartment[]
  mode: AgentFormMode
  /** For clone/edit: the current agent being prefilled or edited */
  editTarget?: RosterAgent
  prefill?: { name: string; description: string; department: string }
  onClose: () => void
  onSaved: (agent: RosterAgent) => void
}

function AgentFormModal({ departments, mode, editTarget, prefill, onClose, onSaved }: AgentFormModalProps) {
  const initial = editTarget
    ? { name: editTarget.name, description: editTarget.description, department: editTarget.department ?? '' }
    : prefill ?? { name: '', description: '', department: '' }

  const [name, setName] = useState(initial.name)
  const [description, setDescription] = useState(initial.description)
  const [department, setDepartment] = useState(initial.department)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameId = 'agent-form-name'
  const descId = 'agent-form-desc'
  const deptId = 'agent-form-dept'
  const errorId = 'agent-form-error'
  const titleId = 'agent-form-title'
  const firstInputRef = useRef<HTMLInputElement>(null)

  const titleByMode: Record<AgentFormMode, string> = {
    create: 'Nuevo agente',
    clone: 'Clonar agente',
    edit: 'Editar agente',
  }

  const submitLabelByMode: Record<AgentFormMode, [string, string]> = {
    create: ['Creando…', 'Crear agente'],
    clone: ['Clonando…', 'Crear copia'],
    edit: ['Guardando…', 'Guardar cambios'],
  }

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
      if (mode === 'edit' && editTarget) {
        const payload: UpdateAgentPayload = {
          name: name.trim(),
          primary_mission: description.trim() || undefined,
          department: department.trim() || undefined,
        }
        const updated = await updateAgent(editTarget.id, payload)
        const rosterAgent: RosterAgent = {
          id: updated.id,
          name: updated.name,
          description: updated.primary_mission ?? '',
          source: 'custom',
          department: department.trim() || '',
          is_default: updated.is_default,
          color: updated.color ?? null,
        }
        onSaved(rosterAgent)
      } else {
        const payload: CreateAgentPayload = {
          name: name.trim(),
          primary_mission: description.trim() || undefined,
          department: department.trim() || undefined,
        }
        const created = await createAgent(payload)
        const rosterAgent: RosterAgent = {
          id: created.id,
          name: created.name,
          description: created.primary_mission ?? '',
          source: 'custom',
          department: department.trim() || '',
          is_default: created.is_default,
          color: created.color ?? null,
        }
        onSaved(rosterAgent)
      }
    } catch (err: unknown) {
      const fallback = mode === 'edit' ? 'Error al guardar el agente.' : 'Error al crear el agente.'
      setError(err instanceof Error ? err.message : fallback)
    } finally {
      setPending(false)
    }
  }

  const [pendingLabel, submitLabel] = submitLabelByMode[mode]

  return (
    <div
      className="office-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="office-modal">
        <div className="office-modal-header">
          <h2 id={titleId} className="office-modal-title">
            {titleByMode[mode]}
          </h2>
          <button
            type="button"
            className="office-modal-close"
            onClick={onClose}
            aria-label="Cerrar"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate className="office-modal-form">
          {mode === 'clone' && (
            <p style={{ fontSize: 'var(--text-label)', color: 'var(--ink3)' }}>
              Copia personalizable de un agente. Puedes modificarla libremente.
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
              {pending ? pendingLabel : submitLabel}
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
  departments: RosterDepartment[]
  isWorking: boolean
  onClose: () => void
  onClone: (agent: RosterAgent) => void
  onRefetch: () => void
}

function AgentDrawer({ agent, departments, isWorking, onClose, onClone, onRefetch }: AgentDrawerProps) {
  const navigate = useNavigate()
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'
  const isDefault = agent.is_default
  const isEditable = !isFactory && !isDefault
  const [activating, setActivating] = useState(false)
  const [showEditModal, setShowEditModal] = useState(false)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function handleChat() {
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

  async function handleDelete() {
    const ok = await confirm({
      title: `¿Eliminar "${agent.name}"?`,
      description: 'El agente se eliminará permanentemente. Esta acción no se puede deshacer.',
      confirmLabel: 'Eliminar',
      variant: 'danger',
    })
    if (!ok) return
    try {
      await deleteAgent(agent.id)
      sileo.success({ title: `${agent.name} eliminado` })
      onClose()
      onRefetch()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'No se pudo eliminar el agente.'
      // 403 means the backend protected it (Cerebro / default agent)
      sileo.error({ title: msg })
    }
  }

  // Strip the "custom:<slug>" namespace the backend uses when displaying the dept label
  const deptLabel = agent.department
    ? agent.department.replace(/^custom:/i, '')
    : ''

  return (
    <>
      {ConfirmDialogNode}
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
            {isDefault && <span className="badge">Cerebro</span>}
            {isFactory && (
              <span
                className="badge"
                style={{
                  background: 'color-mix(in srgb, var(--ok) 15%, transparent)',
                  color: 'var(--ok)',
                }}
              >
                Del sistema
              </span>
            )}
            <button type="button" className="office-modal-close" onClick={onClose} aria-label="Cerrar"><X size={16} aria-hidden="true" /></button>
          </div>

          <div className="office-drawer-body">
            {agent.description && (
              <p className="agent-mission" style={{ marginBottom: 'var(--sp-5)' }}>
                {agent.description}
              </p>
            )}

            {/* Non-editable notice for Cerebro / default agents */}
            {(isDefault || isFactory) && !isEditable && (
              <p style={{
                fontSize: 'var(--text-label)',
                color: 'var(--ink3)',
                marginBottom: 'var(--sp-5)',
                padding: 'var(--sp-3)',
                background: 'var(--surface2)',
                borderRadius: 'var(--r-sm)',
              }}>
                {isDefault
                  ? 'No editable (puedes clonarlo para crear tu propia versión).'
                  : 'Agente del sistema — clónalo para personalizar.'}
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
                title="Activar este agente y abrir el chat"
              >
                {activating ? 'Activando…' : 'Chatear'}
              </button>

              {(isFactory || isDefault) && (
                <button
                  type="button"
                  className="office-btn office-btn--ghost"
                  onClick={() => { onClone(agent); onClose() }}
                >
                  Clonar y personalizar
                </button>
              )}

              {isEditable && (
                <>
                  <button
                    type="button"
                    className="office-btn office-btn--ghost"
                    onClick={() => { navigate('/programadas'); onClose() }}
                  >
                    Programar tarea
                  </button>
                  <button
                    type="button"
                    className="office-btn office-btn--ghost"
                    onClick={() => setShowEditModal(true)}
                  >
                    Editar
                  </button>
                  <button
                    type="button"
                    className="office-btn office-btn--ghost"
                    style={{ color: 'var(--danger)' }}
                    onClick={handleDelete}
                  >
                    Borrar
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      {showEditModal && (
        <AgentFormModal
          departments={departments}
          mode="edit"
          editTarget={agent}
          onClose={() => setShowEditModal(false)}
          onSaved={() => {
            setShowEditModal(false)
            onClose()
            onRefetch()
          }}
        />
      )}
    </>
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
              Del sistema
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
    factory: 'Agentes especializados del sistema — solo lectura.',
    custom: '',
  }

  return (
    <section aria-labelledby={headingId} className="office-section">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--sp-3)' }}>
        <h2 id={headingId} className="office-section-title">{dept.name}</h2>
        {dept.kind === 'factory' && (
          <span style={{ fontSize: 'var(--text-caption)', color: 'var(--ink4)' }}>Sistema</span>
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
  onAgentClick: (agent: RosterAgent) => void
}

interface ClonePrefill {
  name: string
  description: string
  department: string
}

function TarjetasView({ roster, runtimeStatus, hasRuflo, onRosterRefetch, onAgentClick }: TarjetasViewProps) {
  const [showCreateModal, setShowCreateModal] = useState(false)

  const activeIds = activeAgentIds(runtimeStatus)

  const cerebroDepts = roster.departments.filter((d) => d.kind === 'cerebro')
  const factoryDepts = roster.departments.filter((d) => d.kind === 'factory')
  const customDepts = roster.departments.filter((d) => d.kind === 'custom')
  const hasCustomDepts = customDepts.length > 0

  function handleAgentSaved(_agent: RosterAgent) {
    setShowCreateModal(false)
    onRosterRefetch()
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
            onAgentClick={onAgentClick}
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
            onAgentClick={onAgentClick}
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
            onAgentClick={onAgentClick}
            onCreateClick={() => setShowCreateModal(true)}
            sectionIndex={0}
          />
        ))}

        {/* ── System swarm indicator (when detected via MCP but roster doesn't show it) ── */}
        {hasRuflo && factoryDepts.length === 0 && (
          <section aria-labelledby="section-system-swarm" className="office-section">
            <h2 id="section-system-swarm" className="office-section-title">Agentes del sistema</h2>
            <p className="office-section-desc">
              Agentes del sistema conectados — disponibles para el agente en tiempo real.
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
          Lumen
        </p>
      </div>

      {showCreateModal && (
        <AgentFormModal
          departments={roster.departments}
          mode="create"
          onClose={() => setShowCreateModal(false)}
          onSaved={handleAgentSaved}
        />
      )}

    </>
  )
}

// ── OfficeView (root) ─────────────────────────────────────────────────────────

export default function OfficeView() {
  const [tab, setTab] = useState<Tab>('live')
  const [state, dispatch] = useReducer(dataReducer, { status: 'loading' })
  const [showCreateFromHeader, setShowCreateFromHeader] = useState(false)
  const [selectedAgent, setSelectedAgent] = useState<RosterAgent | null>(null)
  const [showCloneModalRoot, setShowCloneModalRoot] = useState(false)
  const [clonePrefillRoot, setClonePrefillRoot] = useState<ClonePrefill | null>(null)
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

  const handleCanvasAgentClick = useCallback((agentId: string, _agentName: string) => {
    if (state.status !== 'ready') return
    const agent = state.roster.departments.flatMap(d => d.agents).find(a => a.id === agentId)
    if (agent) setSelectedAgent(agent)
  }, [state])

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

          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)' }}>
            {state.status === 'ready' && (
              <button
                type="button"
                className="office-btn office-btn--primary"
                style={{ fontSize: 'var(--text-label)', padding: '0 var(--sp-4)', height: 36 }}
                onClick={() => setShowCreateFromHeader(true)}
                aria-label="Crear nuevo agente"
              >
                + Crear agente
              </button>
            )}

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

        {state.status === 'ready' && totalAgentCount === 0 && (
          <div className="state-container" style={{ textAlign: 'center' }}>
            <p className="state-label" style={{ marginBottom: 'var(--sp-4)' }}>
              Aún no tienes agentes.
            </p>
            <button
              type="button"
              className="office-btn office-btn--primary"
              onClick={() => setShowCreateFromHeader(true)}
            >
              Crear tu primer agente
            </button>
          </div>
        )}

        {state.status === 'ready' && totalAgentCount > 0 && (
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
                onAgentClick={setSelectedAgent}
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
                    onAgentClick={handleCanvasAgentClick}
                  />
                </Suspense>
              </div>
            )}
          </>
        )}

        {/* Header-triggered create modal — independent of TarjetasView's own modal */}
        {showCreateFromHeader && state.status === 'ready' && (
          <AgentFormModal
            departments={state.roster.departments}
            mode="create"
            onClose={() => setShowCreateFromHeader(false)}
            onSaved={() => {
              setShowCreateFromHeader(false)
              void load()
            }}
          />
        )}
      </div>

      {/* Agent detail drawer — shared between Tarjetas and Live/Office tabs */}
      {selectedAgent && state.status === 'ready' && (
        <AgentDrawer
          agent={selectedAgent}
          departments={state.roster.departments}
          isWorking={activeAgentIds(state.runtimeStatus).has(selectedAgent.id)}
          onClose={() => setSelectedAgent(null)}
          onClone={(agent) => {
            setSelectedAgent(null)
            setClonePrefillRoot({
              name: `${agent.name} (copia)`,
              description: agent.description,
              department: 'Mis agentes',
            })
            setShowCloneModalRoot(true)
          }}
          onRefetch={load}
        />
      )}

      {showCloneModalRoot && state.status === 'ready' && (
        <AgentFormModal
          departments={state.roster.departments}
          mode="clone"
          prefill={clonePrefillRoot ?? undefined}
          onClose={() => { setShowCloneModalRoot(false); setClonePrefillRoot(null) }}
          onSaved={() => {
            setShowCloneModalRoot(false)
            setClonePrefillRoot(null)
            void load()
          }}
        />
      )}
    </div>
  )
}
