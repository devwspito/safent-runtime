import { lazy, Suspense, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { sileo } from 'sileo'
import { X, AlertTriangle, Users, RefreshCw, Maximize2 } from 'lucide-react'

import { getAgentRoster, getRuntimeStatus, listMcpServers, createAgent, updateAgent, deleteAgent, getDefaultRoster, setDefaultRoster, getAgentStats, openRuntimeStream } from '../api/client'
import type { AgentRoster, RosterAgent, RosterDepartment, RuntimeStatus, CreateAgentPayload, UpdateAgentPayload, AgentStatsResponse } from '../api/types'
import type { LumenAgent, LumenRuntimeStatus } from './office-live/engine/office-state'
import { useConfirmDialog } from '../components/ConfirmDialog'
import { useT } from '../lib/i18n'
import type { ChatOutletContext } from '../components/Layout'
import { AnimatedDrawer, AnimatedPageHeaderText, AnimatePresence, motion, useReducedMotion, SPRING, Stagger, StaggerItem } from '../components/ui/motion'
import { Badge, StatusDot } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'

import styles from './OfficeView.module.css'

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
  | { status: 'ready'; roster: AgentRoster; runtimeStatus: RuntimeStatus; hasRuflo: boolean; agentStats: AgentStatsResponse }

type DataAction =
  | { type: 'LOADED'; roster: AgentRoster; runtimeStatus: RuntimeStatus; hasRuflo: boolean; agentStats: AgentStatsResponse }
  | { type: 'FAILED'; message: string }
  | { type: 'STATUS_UPDATE'; runtimeStatus: RuntimeStatus; agentStats: AgentStatsResponse }

function dataReducer(state: DataState, action: DataAction): DataState {
  switch (action.type) {
    case 'LOADED':
      return { status: 'ready', roster: action.roster, runtimeStatus: action.runtimeStatus, hasRuflo: action.hasRuflo, agentStats: action.agentStats }
    case 'FAILED':
      return { status: 'error', message: action.message }
    case 'STATUS_UPDATE':
      if (state.status !== 'ready') return state
      return { ...state, runtimeStatus: action.runtimeStatus, agentStats: action.agentStats }
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function activeAgentIds(runtimeStatus: RuntimeStatus): Set<string> {
  const ids = new Set<string>()
  if (runtimeStatus.active_agent_id) ids.add(runtimeStatus.active_agent_id)
  for (const a of runtimeStatus.activity ?? []) ids.add(a.agent_id)
  return ids
}

// ── Loading skeleton ──────────────────────────────────────────────────────────

function OfficeSkeleton() {
  return (
    <div className={styles.skeletonWrap} aria-busy="true" aria-label="Cargando equipo…">
      {/* Section 1 */}
      <div className={styles.skeletonSection}>
        <div className={styles.skeletonSectionHead}>
          <div className="skeleton skeleton--line" style={{ width: 120 }} aria-hidden />
          <div className="skeleton skeleton--line-sm" style={{ width: 200 }} aria-hidden />
        </div>
        <div className={styles.skeletonGrid}>
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className={styles.skeletonCard} style={{ animationDelay: `${i * 0.08}s` }}>
              <div className={styles.skeletonCardRow}>
                <div className="skeleton skeleton--avatar" aria-hidden />
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div className="skeleton skeleton--line" style={{ width: '70%' }} aria-hidden />
                  <div className="skeleton skeleton--line-sm" style={{ width: '45%' }} aria-hidden />
                </div>
              </div>
              <div className="skeleton skeleton--line" style={{ width: '90%' }} aria-hidden />
              <div className="skeleton skeleton--line-sm" style={{ width: '60%' }} aria-hidden />
            </div>
          ))}
        </div>
      </div>

      {/* Section 2 */}
      <div className={styles.skeletonSection}>
        <div className={styles.skeletonSectionHead}>
          <div className="skeleton skeleton--line" style={{ width: 90 }} aria-hidden />
        </div>
        <div className={styles.skeletonGrid}>
          {Array.from({ length: 2 }).map((_, i) => (
            <div key={i} className={styles.skeletonCard} style={{ animationDelay: `${(i + 3) * 0.08}s` }}>
              <div className={styles.skeletonCardRow}>
                <div className="skeleton skeleton--avatar" aria-hidden />
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div className="skeleton skeleton--line" style={{ width: '65%' }} aria-hidden />
                  <div className="skeleton skeleton--line-sm" style={{ width: '40%' }} aria-hidden />
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
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
  const [showCustom, setShowCustom] = useState(false)
  const customRef = useRef<HTMLInputElement>(null)

  const existingNames = departments.map((d) => d.name)
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
      {!showInput ? (
        <select
          id={id}
          className={styles.fieldInput}
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
        <div className={styles.deptRow}>
          <input
            ref={customRef}
            id={id}
            type="text"
            className={styles.fieldInput}
            value={value}
            onChange={handleCustomChange}
            placeholder={t('agents.dept.new.placeholder')}
            maxLength={60}
            style={{ flex: 1 }}
          />
          <button
            type="button"
            className={styles.deptClearBtn}
            onClick={handleClear}
            aria-label={t('agents.dept.clear.aria')}
          >
            <X size={14} aria-hidden="true" />
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
      className={styles.modalBackdrop}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h2 id={titleId} className={styles.modalTitle}>
            {titleByMode[mode]}
          </h2>
          <button
            type="button"
            className={styles.modalCloseBtn}
            onClick={onClose}
            aria-label={t('agents.form.close')}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate className={styles.modalForm}>
          {mode === 'clone' && (
            <p className={styles.modalCloneHint}>
              {t('agents.form.clone.hint')}
            </p>
          )}

          <div className={styles.field}>
            <label htmlFor={nameId} className={styles.fieldLabel}>{t('agents.form.name.label')}</label>
            <input
              ref={firstInputRef}
              id={nameId}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={styles.fieldInput}
              required
              aria-required="true"
              aria-describedby={error ? errorId : undefined}
              maxLength={80}
              placeholder={t('agents.form.name.placeholder')}
            />
          </div>

          <div className={styles.field}>
            <label htmlFor={descId} className={styles.fieldLabel}>{t('agents.form.desc.label')}</label>
            <textarea
              id={descId}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={`${styles.fieldInput} ${styles.fieldTextarea}`}
              maxLength={500}
              rows={3}
              placeholder={t('agents.form.desc.placeholder')}
            />
          </div>

          <div className={styles.field}>
            <label htmlFor={deptId} className={styles.fieldLabel}>{t('agents.form.dept.label')}</label>
            <DeptSelector
              id={deptId}
              departments={departments}
              value={department}
              onChange={setDepartment}
            />
          </div>

          {error && (
            <p id={errorId} role="alert" className={styles.fieldError}>
              <AlertTriangle size={13} aria-hidden="true" />
              {error}
            </p>
          )}

          <div className={styles.modalActions}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onClose}
              disabled={pending}
            >
              {t('agents.form.cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              loading={pending}
              aria-busy={pending}
            >
              {pending ? pendingLabel : submitLabel}
            </Button>
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
  open: boolean
  onClose: () => void
  onClone: (agent: RosterAgent) => void
  onRefetch: () => void
}

function AgentDrawer({ agent, departments, isWorking, open, onClose, onClone, onRefetch }: AgentDrawerProps) {
  const t = useT()
  const navigate = useNavigate()
  const { startNewWithAgent } = useOutletContext<ChatOutletContext>()
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'
  const isDefault = agent.is_default
  const isEditable = !isFactory && !isDefault
  const [showEditModal, setShowEditModal] = useState(false)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  // Escape key handled by AnimatedDrawer backdrop
  function handleChat() {
    startNewWithAgent(agent.id, agent.name)
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
      sileo.error({ title: msg })
    }
  }

  const deptLabel = agent.department
    ? agent.department.replace(/^custom:/i, '')
    : ''

  return (
    <>
      {ConfirmDialogNode}
      <AnimatedDrawer
        open={open}
        onBackdropClick={onClose}
        label={agent.name}
        width={380}
      >
        {/* Drawer inner — always rendered inside AnimatedDrawer portal */}
        <div className={styles.drawerHeader}>
          <div
            className={styles.avatar}
            style={{ background: agent.color ?? 'var(--color-accent)' }}
            aria-hidden="true"
          >
            {initials}
          </div>
          <div className={styles.drawerTitleBlock}>
            <h2 id="agent-drawer-title" className={styles.drawerTitle}>{agent.name}</h2>
            {deptLabel && (
              <p className={styles.drawerDept}>{deptLabel}</p>
            )}
          </div>
          <div className={styles.drawerBadges}>
            {isDefault && (
              <Badge variant="default">{t('agents.badge.default')}</Badge>
            )}
            {isFactory && (
              <Badge variant="success">{t('agents.badge.factory')}</Badge>
            )}
          </div>
          <button
            type="button"
            className={styles.drawerCloseBtn}
            onClick={onClose}
            aria-label={t('agents.drawer.close')}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>

        <div className={styles.drawerBody}>
          {agent.description && (
            <p className={styles.drawerMission}>
              {agent.description}
            </p>
          )}

          {(isDefault || isFactory) && !isEditable && (
            <p className={styles.drawerReadonlyNotice}>
              {isDefault
                ? t('agents.drawer.readonly.default')
                : t('agents.drawer.readonly.factory')}
            </p>
          )}

          <div className={styles.drawerStatusRow} aria-live="polite">
            <StatusDot
              state={isWorking ? 'warning' : 'success'}
              label={isWorking ? t('agents.status.working') : t('agents.status.online')}
            />
          </div>

          <div className={styles.drawerActions}>
            <Button
              type="button"
              variant="primary"
              onClick={handleChat}
              title={t('agents.drawer.chat.title')}
            >
              {t('agents.drawer.chat')}
            </Button>

            {(isFactory || isDefault) && (
              <Button
                type="button"
                variant="ghost"
                onClick={() => { onClone(agent); onClose() }}
              >
                {t('agents.drawer.clone')}
              </Button>
            )}

            {isEditable && (
              <>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => { navigate('/programadas'); onClose() }}
                >
                  {t('agents.drawer.schedule')}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setShowEditModal(true)}
                >
                  {t('agents.drawer.edit')}
                </Button>
                <Button
                  type="button"
                  variant="danger"
                  className={styles.drawerActionDanger}
                  onClick={handleDelete}
                >
                  {t('agents.drawer.delete')}
                </Button>
              </>
            )}
          </div>
        </div>
      </AnimatedDrawer>

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
  const reduced = useReducedMotion()
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'
  const deptLabel = agent.department
    ? agent.department.replace(/^custom:/i, '')
    : ''

  const cardClasses = [
    styles.agentCard,
    isWorking ? styles.agentCardWorking : '',
  ].filter(Boolean).join(' ')

  const Inner = (
    <article
      className={cardClasses}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}
      aria-label={isWorking
        ? t('agents.card.aria').replace('{name}', agent.name)
        : t('agents.card.aria_idle').replace('{name}', agent.name)
      }
    >
      <div className={styles.cardHeader}>
        <div
          className={styles.avatar}
          style={{ background: agent.color ?? 'var(--color-accent)' }}
          aria-hidden="true"
        >
          {initials}
        </div>
        <div className={styles.cardMeta}>
          <p className={styles.agentName}>{agent.name}</p>
          {deptLabel && <p className={styles.agentDept}>{deptLabel}</p>}
        </div>
        <div className={styles.cardBadges}>
          {agent.is_default && (
            <Badge variant="default">{t('agents.badge.default')}</Badge>
          )}
          {isFactory && (
            <Badge variant="success">{t('agents.badge.factory')}</Badge>
          )}
          <StatusDot
            state={isWorking ? 'warning' : 'success'}
            label={isWorking ? t('agents.status.working') : t('agents.status.online')}
          />
        </div>
      </div>
      {agent.description && (
        <p className={styles.agentMission}>{agent.description}</p>
      )}
    </article>
  )

  if (reduced) return Inner

  return (
    <motion.div
      whileHover={{ y: -2 }}
      whileTap={{ y: 0 }}
      transition={SPRING}
      style={{ borderRadius: 'var(--radius-md)' }}
    >
      {Inner}
    </motion.div>
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
    <section aria-labelledby={headingId} className={styles.section}>
      <div className={styles.sectionHead}>
        <h2 id={headingId} className={styles.sectionTitle}>{dept.name}</h2>
        {dept.kind === 'factory' && (
          <span className={styles.sectionTag}>{t('agents.dept.factory.tag')}</span>
        )}
      </div>
      {descriptionByKind[dept.kind] && (
        <p className={styles.sectionDesc}>{descriptionByKind[dept.kind]}</p>
      )}

      <Stagger>
        <ul className={styles.agentGrid} role="list">
          {dept.agents.map((a) => (
            <StaggerItem key={a.id} style={{ listStyle: 'none' }}>
              <li style={{ listStyle: 'none' }}>
                <AgentCard
                  agent={a}
                  isWorking={activeIds.has(a.id)}
                  onClick={() => onAgentClick(a)}
                />
              </li>
            </StaggerItem>
          ))}
          {isCustomDept && sectionIndex === 0 && (
            <StaggerItem style={{ listStyle: 'none' }}>
              <li style={{ listStyle: 'none' }}>
                <button
                  type="button"
                  className={styles.createCard}
                  onClick={onCreateClick}
                  aria-label={t('agents.card.create.aria')}
                >
                  <span className={styles.createIcon} aria-hidden="true">+</span>
                  <span className={styles.createLabel}>{t('agents.card.create.label')}</span>
                </button>
              </li>
            </StaggerItem>
          )}
        </ul>
      </Stagger>
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
      <div className={styles.tarjetasBody}>
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

        {!hasCustomDepts && (
          <section aria-labelledby="section-mis-agentes" className={styles.section}>
            <h2 id="section-mis-agentes" className={styles.sectionTitle}>{t('agents.dept.mine.title')}</h2>
            <div className={styles.mineEmpty}>
              <p className={styles.mineEmptyText}>{t('agents.dept.mine.empty')}</p>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setShowCreateModal(true)}
              >
                + {t('agents.card.create.label')}
              </Button>
            </div>
          </section>
        )}

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

        {hasRuflo && factoryDepts.length === 0 && (
          <section aria-labelledby="section-system-swarm" className={styles.section}>
            <h2 id="section-system-swarm" className={styles.sectionTitle}>{t('agents.dept.swarm.title')}</h2>
            <p className={styles.swarmDesc}>
              {t('agents.dept.swarm.desc')}
              {runtimeStatus.ruflo_active && (
                <StatusDot
                  state="success"
                  label={t('agents.dept.swarm.active')}
                />
              )}
            </p>
          </section>
        )}

        <p className={styles.attribution}>Lumen</p>
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

  // ── Default roster toggle ────────────────────────────────────────────────
  const [defaultRosterEnabled, setDefaultRosterEnabled] = useState(true)
  const [rosterTogglePending, setRosterTogglePending] = useState(false)

  // ── Initial load + programmatic refetch ──────────────────────────────────
  const load = useCallback(async () => {
    try {
      const [roster, runtimeStatus, mcpServers, agentStats] = await Promise.all([
        getAgentRoster(),
        getRuntimeStatus(),
        listMcpServers(),
        getAgentStats(),
      ])
      const hasRuflo = mcpServers.some((s) => s.slug === 'ruflo')
      dispatch({ type: 'LOADED', roster, runtimeStatus, hasRuflo, agentStats })
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : t('agents.loading')
      dispatch({ type: 'FAILED', message })
    }
  }, [t])

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const [roster, runtimeStatus, mcpServers, defaultRoster, agentStats] = await Promise.all([
          getAgentRoster(),
          getRuntimeStatus(),
          listMcpServers(),
          getDefaultRoster(),
          getAgentStats(),
        ])
        if (!cancelled) {
          const hasRuflo = mcpServers.some((s) => s.slug === 'ruflo')
          dispatch({ type: 'LOADED', roster, runtimeStatus, hasRuflo, agentStats })
          setDefaultRosterEnabled(defaultRoster.enabled)
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

  // ── Runtime status + agent stats: live SSE push (replaces the 4 s poll) ──
  useEffect(() => {
    const close = openRuntimeStream(({ runtime, stats }) => {
      dispatch({ type: 'STATUS_UPDATE', runtimeStatus: runtime, agentStats: stats })
    })
    return close
  }, [])

  const handleCanvasAgentClick = useCallback((agentId: string, _agentName: string) => {
    if (state.status !== 'ready') return
    const agent = state.roster.departments.flatMap(d => d.agents).find(a => a.id === agentId)
    if (agent) setSelectedAgent(agent)
  }, [state])

  // Fullscreen for the live floor — canvas wrap owns fullscreen, not the entire view.
  const canvasWrapRef = useRef<HTMLDivElement>(null)
  const toggleFullscreen = useCallback(() => {
    const el = canvasWrapRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen?.()
  }, [])

  async function handleDefaultRosterToggle() {
    const next = !defaultRosterEnabled
    setDefaultRosterEnabled(next)
    setRosterTogglePending(true)
    try {
      await setDefaultRoster(next)
      await load()
    } catch {
      setDefaultRosterEnabled(!next)
      sileo.error({ title: t('agents.roster.toggle.err') })
    } finally {
      setRosterTogglePending(false)
    }
  }

  // Convert roster agents to the engine's LumenAgent shape for the canvas.
  const engineAgents: LumenAgent[] = state.status === 'ready'
    ? state.roster.departments.flatMap((dept) =>
        dept.agents.map((a) => rosterAgentToLumenAgent(a, dept))
      )
    : []

  const totalAgentCount = engineAgents.length

  const engineRuntimeStatus: LumenRuntimeStatus = state.status === 'ready'
    ? state.runtimeStatus
    : { state: 'idle', active_task_count: 0 }

  const isOnline = state.status === 'ready'
    && (engineRuntimeStatus.state === 'working' || engineRuntimeStatus.active_task_count > 0)

  const activeTasks = state.status === 'ready'
    ? (engineRuntimeStatus.active_task_count ?? 0)
    : 0

  // Active agents for the legend (up to 4 shown)
  const activeIds = state.status === 'ready' ? activeAgentIds(state.runtimeStatus) : new Set<string>()
  const activeAgentsList = state.status === 'ready'
    ? state.roster.departments.flatMap(d => d.agents).filter(a => activeIds.has(a.id)).slice(0, 4)
    : []

  const subtitle = state.status === 'ready'
    ? (totalAgentCount === 1
        ? t('agents.subtitle.ready').replace('{count}', String(totalAgentCount))
        : t('agents.subtitle.ready_pl').replace('{count}', String(totalAgentCount))
      ) + t('agents.subtitle.suffix')
    : t('agents.subtitle.loading') + t('agents.subtitle.suffix')

  return (
    <div className={styles.officeView}>
      {/* ── Header ── */}
      <header className="view-header office-view-header">
        <div className={styles.headerRow}>
          <div className={styles.headerLeft}>
            <AnimatedPageHeaderText
              title={t('view.agentes')}
              subtitle={state.status !== 'loading' ? subtitle : undefined}
            />
          </div>

          <div className={styles.headerActions}>
            {/* Default roster toggle */}
            <div
              className={styles.rosterToggleRow}
              title={t('agents.roster.toggle.tooltip')}
            >
              <span className={styles.rosterToggleLabel}>
                {t('agents.roster.toggle.label')}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={defaultRosterEnabled}
                aria-label={t('agents.roster.toggle.label')}
                className={`seg-pol-switch${defaultRosterEnabled ? ' seg-pol-switch--on' : ''}`}
                onClick={handleDefaultRosterToggle}
                disabled={rosterTogglePending}
              />
            </div>

            {state.status === 'ready' && (
              <Button
                type="button"
                variant="primary"
                size="sm"
                onClick={() => setShowCreateFromHeader(true)}
                aria-label={t('agents.create.aria')}
              >
                {t('agents.create.btn')}
              </Button>
            )}

            <div className={styles.segToggle} role="group" aria-label={t('agents.tab.aria')}>
              <button
                type="button"
                className={`${styles.segBtn}${tab === 'tarjetas' ? ` ${styles.segBtnActive}` : ''}`}
                onClick={() => setTab('tarjetas')}
                aria-pressed={tab === 'tarjetas'}
              >
                {t('agents.tab.cards')}
              </button>
              <button
                type="button"
                className={`${styles.segBtn}${tab === 'live' ? ` ${styles.segBtnActive}` : ''}`}
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
      <div className={styles.body}>
        {/* Loading skeleton */}
        {state.status === 'loading' && <OfficeSkeleton />}

        {/* Error */}
        {state.status === 'error' && (
          <div className={styles.errorState} role="alert">
            <span className={styles.errorIcon} aria-hidden="true">
              <AlertTriangle size={18} />
            </span>
            <p className={styles.errorTitle}>No se pudo cargar el equipo</p>
            <p className={styles.errorMessage}>{state.message}</p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => void load()}
            >
              <RefreshCw size={13} aria-hidden="true" style={{ marginRight: 6 }} />
              Reintentar
            </Button>
          </div>
        )}

        {/* Empty — no agents */}
        {state.status === 'ready' && totalAgentCount === 0 && (
          <div className={styles.emptyState}>
            <div className="ds-empty-state">
              <span className="ds-empty-state__icon" aria-hidden="true">
                <Users size={28} />
              </span>
              <p className="ds-empty-state__title">{t('agents.empty.text')}</p>
              <div className="ds-empty-state__action">
                <Button
                  type="button"
                  variant="primary"
                  onClick={() => setShowCreateFromHeader(true)}
                >
                  {t('agents.empty.cta')}
                </Button>
              </div>
            </div>
          </div>
        )}

        {/* Main content */}
        {state.status === 'ready' && totalAgentCount > 0 && (
          <>
            <AnimatePresence mode="wait">
              {tab === 'tarjetas' && (
                <motion.div
                  key="tarjetas"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.14 }}
                  style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
                >
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
                        agentStats: state.agentStats,
                      })
                    }
                    onRosterRefetch={load}
                    onAgentClick={setSelectedAgent}
                  />
                </motion.div>
              )}

              {tab === 'live' && (
                <motion.div
                  key="live"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.14 }}
                  style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
                >
                  <div className={styles.liveFrame}>
                    {/* Toolbar */}
                    <div className={styles.liveToolbar}>
                      <div className={styles.liveToolbarLeft}>
                        {/* Live/offline chip */}
                        <span className={`${styles.liveChip} ${isOnline ? styles.liveChipOnline : styles.liveChipOffline}`}>
                          <span className={styles.livePulse} aria-hidden="true" />
                          {isOnline ? 'En vivo' : 'En espera'}
                        </span>

                        {/* Active task count */}
                        {activeTasks > 0 && (
                          <span className={styles.taskBadge} aria-label={`${activeTasks} tarea${activeTasks !== 1 ? 's' : ''} activa${activeTasks !== 1 ? 's' : ''}`}>
                            <span className={`${styles.num}`}>{activeTasks}</span>
                            {activeTasks === 1 ? ' tarea' : ' tareas'}
                          </span>
                        )}

                        {/* Active agents legend */}
                        {activeAgentsList.length > 0 && (
                          <>
                            <span className={styles.legendSep} aria-hidden="true" />
                            <div className={styles.legend} aria-label="Agentes activos">
                              {activeAgentsList.map((a) => (
                                <span key={a.id} className={styles.legendItem}>
                                  <span
                                    className={`${styles.legendDot} ${styles.legendDotActive}`}
                                    style={{ background: a.color ?? 'var(--color-warning)' }}
                                    aria-hidden="true"
                                  />
                                  <span className="truncate">{a.name}</span>
                                </span>
                              ))}
                            </div>
                          </>
                        )}

                        {/* Idle legend */}
                        {activeAgentsList.length === 0 && totalAgentCount > 0 && (
                          <span className={styles.legendItem}>
                            <span className={`${styles.legendDot} ${styles.legendDotIdle}`} aria-hidden="true" />
                            <span className="text-dim">
                              <span className={styles.num}>{totalAgentCount}</span>
                              {totalAgentCount === 1 ? ' agente disponible' : ' agentes disponibles'}
                            </span>
                          </span>
                        )}
                      </div>

                      <div className={styles.liveToolbarRight}>
                        <button
                          type="button"
                          className={styles.fullscreenBtn}
                          onClick={toggleFullscreen}
                          aria-label={t('agents.fullscreen')}
                          title={t('agents.fullscreen')}
                        >
                          <Maximize2 size={13} aria-hidden="true" />
                        </button>
                      </div>
                    </div>

                    {/* Canvas */}
                    <div className={styles.canvasWrap} ref={canvasWrapRef}>
                      <Suspense fallback={
                        <div className={styles.canvasFallback} aria-busy="true" aria-label="Cargando plano de oficina…">
                          <div className={styles.canvasFallbackGrid} aria-hidden="true">
                            {Array.from({ length: 6 }).map((_, i) => (
                              <div key={i} className={styles.canvasFallbackCell} />
                            ))}
                          </div>
                          <p className={styles.canvasFallbackText}>Cargando plano de oficina…</p>
                        </div>
                      }>
                        <OfficeCanvas
                          agents={engineAgents}
                          runtimeStatus={engineRuntimeStatus}
                          onAgentClick={handleCanvasAgentClick}
                          agentStats={state.status === 'ready' ? state.agentStats : undefined}
                        />
                      </Suspense>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </>
        )}

        {/* Header-triggered create modal */}
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
      {state.status === 'ready' && (
        <AgentDrawer
          agent={selectedAgent ?? state.roster.departments[0]?.agents[0] ?? ({ id: '', name: '', description: '', source: 'custom', is_default: false, department: '', color: null } as RosterAgent)}
          departments={state.roster.departments}
          isWorking={selectedAgent ? activeAgentIds(state.runtimeStatus).has(selectedAgent.id) : false}
          open={selectedAgent !== null}
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
