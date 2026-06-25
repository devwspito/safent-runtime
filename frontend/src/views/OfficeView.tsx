import { lazy, Suspense, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'
import { X } from 'lucide-react'

import { getAgentRoster, getRuntimeStatus, listMcpServers, createAgent, setActiveAgent, updateAgent, deleteAgent } from '../api/client'
import type { AgentRoster, RosterAgent, RosterDepartment, RuntimeStatus, CreateAgentPayload, UpdateAgentPayload } from '../api/types'
import type { LumenAgent, LumenRuntimeStatus } from './office-live/engine/office-state'
import { useConfirmDialog } from '../components/ConfirmDialog'
import { useT } from '../lib/i18n'

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
  const t = useT()
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
          <option value="">{t('agents.dept.none')}</option>
          {existingNames.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
          <option value="__new__">{t('agents.dept.new')}</option>
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
            placeholder={t('agents.dept.new.placeholder')}
            maxLength={60}
            style={{ flex: 1 }}
          />
          <button
            type="button"
            className="office-btn office-btn--ghost"
            style={{ height: 36, padding: '0 var(--sp-3)', fontSize: 'var(--text-label)' }}
            onClick={handleClear}
            aria-label={t('agents.dept.clear.aria')}
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
  const t = useT()
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
    create: t('agents.form.title.create'),
    clone:  t('agents.form.title.clone'),
    edit:   t('agents.form.title.edit'),
  }

  useEffect(() => {
    firstInputRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) { setError(t('agents.form.err.name')); return }
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
      const fallback = mode === 'edit' ? t('agents.form.err.edit') : t('agents.form.err.create')
      setError(err instanceof Error ? err.message : fallback)
    } finally {
      setPending(false)
    }
  }

  const pendingLabel = mode === 'create'
    ? t('agents.form.submit.creating')
    : mode === 'clone'
      ? t('agents.form.submit.cloning')
      : t('agents.form.submit.saving')

  const submitLabel = mode === 'create'
    ? t('agents.form.submit.create')
    : mode === 'clone'
      ? t('agents.form.submit.clone')
      : t('agents.form.submit.edit')

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
            aria-label={t('agents.form.close')}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate className="office-modal-form">
          {mode === 'clone' && (
            <p style={{ fontSize: 'var(--text-label)', color: 'var(--ink3)' }}>
              {t('agents.form.clone.hint')}
            </p>
          )}

          <div className="office-field">
            <label htmlFor={nameId} className="office-field-label">{t('agents.form.name.label')}</label>
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
              placeholder={t('agents.form.name.placeholder')}
            />
          </div>

          <div className="office-field">
            <label htmlFor={descId} className="office-field-label">{t('agents.form.desc.label')}</label>
            <textarea
              id={descId}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="office-field-input office-field-textarea"
              maxLength={500}
              rows={3}
              placeholder={t('agents.form.desc.placeholder')}
            />
          </div>

          <div className="office-field">
            <label htmlFor={deptId} className="office-field-label">{t('agents.form.dept.label')}</label>
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
              {t('agents.form.cancel')}
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
  const t = useT()
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
        sileo.success({ title: t('agents.drawer.toast.activated').replace('{name}', agent.name) })
      } catch {
        sileo.warning({ title: t('agents.drawer.toast.activate_err').replace('{name}', agent.name) })
      } finally {
        setActivating(false)
      }
    }
    navigate('/chat')
    onClose()
  }

  async function handleDelete() {
    const ok = await confirm({
      title: t('agents.drawer.confirm.title').replace('{name}', agent.name),
      description: t('agents.drawer.confirm.desc'),
      confirmLabel: t('agents.drawer.confirm.confirm'),
      variant: 'danger',
    })
    if (!ok) return
    try {
      await deleteAgent(agent.id)
      sileo.success({ title: t('agents.drawer.toast.deleted').replace('{name}', agent.name) })
      onClose()
      onRefetch()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : t('agents.drawer.toast.delete_err')
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
            {isDefault && <span className="badge">{t('agents.badge.default')}</span>}
            {isFactory && (
              <span
                className="badge"
                style={{
                  background: 'color-mix(in srgb, var(--ok) 15%, transparent)',
                  color: 'var(--ok)',
                }}
              >
                {t('agents.badge.factory')}
              </span>
            )}
            <button type="button" className="office-modal-close" onClick={onClose} aria-label={t('agents.drawer.close')}><X size={16} aria-hidden="true" /></button>
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
                  ? t('agents.drawer.readonly.default')
                  : t('agents.drawer.readonly.factory')}
              </p>
            )}

            <div className="office-drawer-status">
              <span
                className="office-status-dot"
                style={{ background: isWorking ? 'var(--warn)' : 'var(--ok)' }}
                aria-hidden="true"
              />
              <span style={{ fontSize: 'var(--text-label)', color: 'var(--ink3)' }}>
                {isWorking ? t('agents.status.working') : t('agents.status.online')}
              </span>
            </div>

            <div className="office-drawer-actions">
              <button
                type="button"
                className="office-btn office-btn--primary"
                onClick={handleChat}
                disabled={activating}
                aria-busy={activating}
                title={t('agents.drawer.chat.title')}
              >
                {activating ? t('agents.drawer.activating') : t('agents.drawer.chat')}
              </button>

              {(isFactory || isDefault) && (
                <button
                  type="button"
                  className="office-btn office-btn--ghost"
                  onClick={() => { onClone(agent); onClose() }}
                >
                  {t('agents.drawer.clone')}
                </button>
              )}

              {isEditable && (
                <>
                  <button
                    type="button"
                    className="office-btn office-btn--ghost"
                    onClick={() => { navigate('/programadas'); onClose() }}
                  >
                    {t('agents.drawer.schedule')}
                  </button>
                  <button
                    type="button"
                    className="office-btn office-btn--ghost"
                    onClick={() => setShowEditModal(true)}
                  >
                    {t('agents.drawer.edit')}
                  </button>
                  <button
                    type="button"
                    className="office-btn office-btn--ghost"
                    style={{ color: 'var(--danger)' }}
                    onClick={handleDelete}
                  >
                    {t('agents.drawer.delete')}
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
  const t = useT()
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'

  return (
    <article
      className="agent-card office-agent-card"
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}
      aria-label={isWorking
        ? t('agents.card.aria').replace('{name}', agent.name)
        : t('agents.card.aria_idle').replace('{name}', agent.name)
      }
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
          {agent.is_default && <span className="badge">{t('agents.badge.default')}</span>}
          {isFactory && (
            <span
              className="badge"
              style={{
                background: 'color-mix(in srgb, var(--ok) 15%, transparent)',
                color: 'var(--ok)',
                fontSize: 'var(--text-micro)',
              }}
            >
              {t('agents.badge.factory')}
            </span>
          )}
          <span
            className="office-status-dot"
            style={{ background: isWorking ? 'var(--warn)' : 'var(--ok)' }}
            aria-label={isWorking ? t('agents.status.working') : t('agents.status.online')}
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
  const t = useT()
  const headingId = `section-dept-${dept.id}`
  const isCustomDept = dept.kind === 'custom'

  const descriptionByKind: Record<string, string> = {
    cerebro: t('agents.dept.cerebro.desc'),
    factory: t('agents.dept.factory.desc'),
    custom: '',
  }

  return (
    <section aria-labelledby={headingId} className="office-section">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--sp-3)' }}>
        <h2 id={headingId} className="office-section-title">{dept.name}</h2>
        {dept.kind === 'factory' && (
          <span style={{ fontSize: 'var(--text-caption)', color: 'var(--ink4)' }}>{t('agents.dept.factory.tag')}</span>
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
              aria-label={t('agents.card.create.aria')}
            >
              <span className="office-create-icon" aria-hidden="true">+</span>
              <span className="office-create-label">{t('agents.card.create.label')}</span>
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
  const t = useT()
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
            <h2 id="section-mis-agentes" className="office-section-title">{t('agents.dept.mine.title')}</h2>
            <p className="state-label" style={{ padding: 0 }}>{t('agents.dept.mine.empty')}</p>
            <button
              type="button"
              className="office-btn office-btn--ghost"
              style={{ marginTop: 'var(--sp-4)', alignSelf: 'flex-start' }}
              onClick={() => setShowCreateModal(true)}
            >
              + {t('agents.card.create.label')}
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
            <h2 id="section-system-swarm" className="office-section-title">{t('agents.dept.swarm.title')}</h2>
            <p className="office-section-desc">
              {t('agents.dept.swarm.desc')}
              {runtimeStatus.ruflo_active && (
                <span
                  className="office-status-dot"
                  style={{ background: 'var(--ok)', marginLeft: 8, verticalAlign: 'middle' }}
                  aria-label={t('agents.dept.swarm.active')}
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
  const t = useT()
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
      const message = err instanceof Error ? err.message : t('agents.loading')
      dispatch({ type: 'FAILED', message })
    }
  }, [t])

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
          const message = err instanceof Error ? err.message : t('agents.loading')
          dispatch({ type: 'FAILED', message })
        }
      }
    }
    void run()
    return () => { cancelled = true }
  }, [t])

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

  const subtitle = state.status === 'ready'
    ? (totalAgentCount === 1
        ? t('agents.subtitle.ready').replace('{count}', String(totalAgentCount))
        : t('agents.subtitle.ready_pl').replace('{count}', String(totalAgentCount))
      ) + t('agents.subtitle.suffix')
    : t('agents.subtitle.loading') + t('agents.subtitle.suffix')

  return (
    <div className="office-view">
      {/* ── Header with segmented toggle ── */}
      <header className="view-header office-view-header">
        <div className="office-header-row">
          <div>
            <h1 className="view-title">{t('view.agentes')}</h1>
            <p className="view-subtitle">{subtitle}</p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)' }}>
            {state.status === 'ready' && (
              <button
                type="button"
                className="office-btn office-btn--primary"
                style={{ fontSize: 'var(--text-label)', padding: '0 var(--sp-4)', height: 36 }}
                onClick={() => setShowCreateFromHeader(true)}
                aria-label={t('agents.create.aria')}
              >
                {t('agents.create.btn')}
              </button>
            )}

            <div className="office-seg-toggle" role="group" aria-label={t('agents.tab.aria')}>
              <button
                type="button"
                className={`office-seg-btn${tab === 'tarjetas' ? ' office-seg-btn--active' : ''}`}
                onClick={() => setTab('tarjetas')}
                aria-pressed={tab === 'tarjetas'}
              >
                {t('agents.tab.cards')}
              </button>
              <button
                type="button"
                className={`office-seg-btn${tab === 'live' ? ' office-seg-btn--active' : ''}`}
                onClick={() => setTab('live')}
                aria-pressed={tab === 'live'}
              >
                {t('agents.tab.live')}
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* ── Body ── */}
      <div className="office-body">
        {state.status === 'loading' && (
          <div className="state-container" aria-live="polite" aria-busy="true">
            <p className="state-label">{t('agents.loading')}</p>
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
              {t('agents.empty.text')}
            </p>
            <button
              type="button"
              className="office-btn office-btn--primary"
              onClick={() => setShowCreateFromHeader(true)}
            >
              {t('agents.empty.cta')}
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
                  aria-label={t('agents.fullscreen')}
                  title={t('agents.fullscreen')}
                >⛶</button>
                <Suspense fallback={
                  <div className="state-container">
                    <p className="state-label">{t('agents.map.loading')}</p>
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
              name: `${agent.name}${t('agents.clone.name_suffix')}`,
              description: agent.description,
              department: t('agents.clone.default_dept'),
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
