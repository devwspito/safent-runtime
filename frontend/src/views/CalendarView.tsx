import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { listConfiguredTasks, listRecentTasks, createTask, deleteTask, toggleTask, listAgents, ApiError } from '../api/client'
import type { ConfiguredTask, RecentTask, Agent, CreateTaskPayload } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'

// ── Cron parsing (mirrors tasks-view.js exactly) ──────────────────────────────

const DOW_LABELS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'] as const

function expandCronField(field: string | undefined, lo: number, hi: number): Set<number> {
  const out = new Set<number>()
  if (!field || field === '*' || field === '?') {
    for (let i = lo; i <= hi; i++) out.add(i)
    return out
  }
  for (const part of field.split(',')) {
    const stepM = part.match(/^(.+)\/(\d+)$/)
    const range = stepM ? stepM[1] : part
    const step = stepM ? parseInt(stepM[2], 10) : 1
    let a: number, b: number
    if (range === '*') { a = lo; b = hi }
    else {
      const rm = range.match(/^(\d+)-(\d+)$/)
      if (rm) { a = +rm[1]; b = +rm[2] }
      else if (/^\d+$/.test(range)) { a = b = +range }
      else continue
    }
    for (let i = a; i <= b; i += (step || 1)) out.add(i)
  }
  return out
}

interface CronInfo { days: Set<number>; time: string; daily: boolean; valid: boolean }

function parseCron(cron: string | undefined): CronInfo {
  if (!cron) return { days: new Set(), time: '', daily: true, valid: false }
  const parts = cron.trim().split(/\s+/)
  if (parts.length < 5) return { days: new Set(), time: '', daily: true, valid: false }
  const [min, hour, , , dow] = parts
  let time = ''
  if (/^\d+$/.test(hour)) {
    time = `${String(hour).padStart(2, '0')}:${/^\d+$/.test(min) ? String(min).padStart(2, '0') : '00'}`
  }
  const dowAny = dow === '*' || dow === '?'
  const days = new Set<number>()
  if (!dowAny) {
    expandCronField(dow, 0, 7).forEach(d => {
      const sun = d === 7 ? 0 : d
      days.add((sun + 6) % 7)  // Mon=0..Sun=6
    })
  }
  return { days, time, daily: dowAny, valid: true }
}

function taskCron(task: ConfiguredTask): string {
  return task.recurrence ?? task.cron ?? task.schedule ?? task.trigger?.cron ?? ''
}

function buildCron({ mode, days, date, time }: {
  mode: 'recurrent' | 'once'; days: number[]; date: string; time: string
}): string {
  const [hh, mm] = (time || '09:00').split(':')
  const min = parseInt(mm, 10) || 0
  const hour = parseInt(hh, 10) || 0
  if (mode === 'once') {
    // Carry yyyy-mm-dd in full so tasksForDate can match the exact year.
    // Store as "min hour dd mo yyyy" (non-standard 6-field) — tasksForDate
    // and the backend use next_run_at for scheduling; this is only for the UI.
    const [yyyy, mo, dd] = (date || '').split('-')
    return `${min} ${hour} ${parseInt(dd, 10)} ${parseInt(mo, 10)} * ${parseInt(yyyy, 10)}`
  }
  if (days.length === 0 || days.length === 7) return `${min} ${hour} * * *`
  const dow = days.map(d => (d === 6 ? 0 : d + 1)).sort((a, b) => a - b).join(',')
  return `${min} ${hour} * * ${dow}`
}

const pad2 = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`

function tasksForDate(date: Date, tasks: ConfiguredTask[]): Array<{ task: ConfiguredTask; time: string }> {
  const out: Array<{ task: ConfiguredTask; time: string }> = []
  const monIdx = (date.getDay() + 6) % 7
  const dd = date.getDate(), mo = date.getMonth() + 1, yyyy = date.getFullYear()
  for (const tk of tasks) {
    const cron = taskCron(tk)
    if (tk.one_shot) {
      // Prefer backend next_run_at for exact matching (avoids year ambiguity)
      if (tk.next_run_at) {
        const nr = new Date(tk.next_run_at)
        if (nr.getDate() === dd && nr.getMonth() + 1 === mo && nr.getFullYear() === yyyy) {
          out.push({ task: tk, time: `${String(nr.getHours()).padStart(2,'0')}:${String(nr.getMinutes()).padStart(2,'0')}` })
        }
        continue
      }
      // Fallback: parse the 6-field cron we build (min hour dd mo * yyyy)
      const p = (cron || '').trim().split(/\s+/)
      const cronDd = parseInt(p[2] ?? '', 10)
      const cronMo = parseInt(p[3] ?? '', 10)
      const cronYyyy = p.length >= 6 ? parseInt(p[5] ?? '', 10) : NaN
      const yearMatches = isNaN(cronYyyy) || cronYyyy === yyyy
      if (cronDd === dd && cronMo === mo && yearMatches) {
        out.push({ task: tk, time: parseCron(cron).time })
      }
    } else {
      const { days, time, daily, valid } = parseCron(cron)
      if (valid && (daily || days.has(monIdx))) out.push({ task: tk, time })
    }
  }
  return out
}

const AGENT_HUES = [210, 145, 280, 30, 340, 190, 95, 255]
function agentHue(id: string): number {
  let h = 0; const s = String(id ?? '')
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return AGENT_HUES[h % AGENT_HUES.length]
}

function relativeTime(iso: string | undefined): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'Ahora'
  if (mins < 60) return `Hace ${mins} min`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `Hace ${hrs}h`
  return `Hace ${Math.floor(hrs / 24)}d`
}

function statusMeta(status: string): { label: string } {
  const s = String(status).toLowerCase()
  if (s === 'completed' || s === 'done' || s === 'success') return { label: 'Completada' }
  if (s === 'in_progress' || s === 'running' || s === 'claimed') return { label: 'En curso' }
  if (s === 'failed' || s === 'error') return { label: 'Falló' }
  return { label: status }
}

// ── State types ───────────────────────────────────────────────────────────────

type ViewMode = 'board' | 'list'

interface CalState {
  tasks: ConfiguredTask[]
  recentTasks: RecentTask[]
  agents: Agent[]
  loading: boolean
  error: string | null
}

type CalAction =
  | { type: 'LOADED'; tasks: ConfiguredTask[]; recentTasks: RecentTask[]; agents: Agent[] }
  | { type: 'TASKS_LOADED'; tasks: ConfiguredTask[] }
  | { type: 'RECENT_LOADED'; recentTasks: RecentTask[] }
  | { type: 'FAILED'; error: string }

function calReducer(s: CalState, a: CalAction): CalState {
  switch (a.type) {
    case 'LOADED': return { ...s, tasks: a.tasks, recentTasks: a.recentTasks, agents: a.agents, loading: false, error: null }
    case 'TASKS_LOADED': return { ...s, tasks: a.tasks, loading: false }
    case 'RECENT_LOADED': return { ...s, recentTasks: a.recentTasks }
    case 'FAILED': return { ...s, loading: false, error: a.error }
  }
}

function show(message: string, kind: 'ok' | 'warn' | 'error' = 'ok') {
  if (kind === 'ok') sileo.success({ title: message })
  else if (kind === 'error') sileo.error({ title: message })
  else sileo.warning({ title: message })
}

export default function CalendarView() {
  const [state, dispatch] = useReducer(calReducer, {
    tasks: [], recentTasks: [], agents: [], loading: true, error: null,
  })
  const [viewMode, setViewMode] = useState<ViewMode>('board')
  const [calRef, setCalRef] = useState<Date>(() => { const n = new Date(); return new Date(n.getFullYear(), n.getMonth(), 1) })
  const [modalOpen, setModalOpen] = useState(false)
  const [modalPresetDate, setModalPresetDate] = useState<string | null>(null)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  const agentsById = Object.fromEntries(state.agents.map(a => [a.id, a]))

  function agentLabel(task: ConfiguredTask): string {
    const id = task.target_agent_id ?? task.agent_id ?? ''
    if (!id) return 'Todos los agentes'
    const a = agentsById[id]
    if (a?.is_default) return 'Agente principal'
    return a?.name ?? 'Agente principal'
  }

  const loadAll = useCallback(async () => {
    try {
      const [configRes, recentRes, agents] = await Promise.all([
        listConfiguredTasks(),
        listRecentTasks(20),
        listAgents().catch(() => [] as Agent[]),
      ])
      dispatch({
        type: 'LOADED',
        tasks: configRes?.tasks ?? [],
        recentTasks: recentRes?.tasks ?? [],
        agents: Array.isArray(agents) ? agents : [],
      })
    } catch (e) {
      dispatch({ type: 'FAILED', error: e instanceof ApiError ? e.message : 'No se pudieron cargar las tareas.' })
    }
  }, [])

  async function reloadTasks() {
    const [configRes, recentRes] = await Promise.all([listConfiguredTasks(), listRecentTasks(20)])
    dispatch({ type: 'TASKS_LOADED', tasks: configRes?.tasks ?? [] })
    dispatch({ type: 'RECENT_LOADED', recentTasks: recentRes?.tasks ?? [] })
  }

  useEffect(() => { loadAll() }, [loadAll])

  function openModal(presetDate: string | null = null) {
    setModalPresetDate(presetDate)
    setModalOpen(true)
  }

  async function handleToggle(task: ConfiguredTask) {
    const id = task.trigger_id ?? task.task_id ?? task.id ?? ''
    try {
      await toggleTask(id, task.enabled === false)
      show(task.enabled !== false ? 'Tarea pausada' : 'Tarea activada', 'ok')
      reloadTasks()
    } catch (e) { show(e instanceof Error ? e.message : 'Error', 'error') }
  }

  async function handleDelete(task: ConfiguredTask) {
    const id = task.trigger_id ?? task.task_id ?? task.id ?? ''
    const name = task.label ?? task.name ?? id
    const ok = await confirm({
      title: `¿Eliminar "${name}"?`,
      description: 'Esta tarea programada no se ejecutará más.',
      confirmLabel: 'Eliminar',
      variant: 'danger',
    })
    if (!ok) return
    try {
      await deleteTask(id)
      show('Tarea eliminada', 'ok')
      reloadTasks()
    } catch (e) { show(e instanceof Error ? e.message : 'Error', 'error') }
  }

  return (
    <>
      {ConfirmDialogNode}
      <header className="view-header">
        <h1 className="view-title">Tareas programadas</h1>
        <p className="view-subtitle">Agenda programada y cola de ejecución del agente.</p>
      </header>

      <div className="view-body cv-view-body">
        {/* ── Scheduled tasks section ─────────────────────────────────────── */}
        <section className="cv-section" aria-label="Tareas programadas">
          <div className="cv-section-head">
            <h2 className="cv-section-label">Programadas</h2>
            <div className="cv-section-head__right">
              <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={() => openModal()}>
                + Nueva tarea
              </button>
              <div className="seg-toggle" role="tablist" aria-label="Vista">
                <button
                  className={`seg-toggle__btn${viewMode === 'board' ? ' is-active' : ''}`}
                  role="tab"
                  aria-selected={viewMode === 'board'}
                  onClick={() => setViewMode('board')}
                >
                  Calendario
                </button>
                <button
                  className={`seg-toggle__btn${viewMode === 'list' ? ' is-active' : ''}`}
                  role="tab"
                  aria-selected={viewMode === 'list'}
                  onClick={() => setViewMode('list')}
                >
                  Lista
                </button>
              </div>
            </div>
          </div>

          {state.loading && <div className="cv-skeleton" aria-busy="true" />}
          {state.error && <p className="state-error" role="alert">{state.error}</p>}

          {!state.loading && !state.error && (
            <>
              {/* ── Month calendar ─────────────────────────────────────────── */}
              {viewMode === 'board' && (
                <MonthCalendar
                  tasks={state.tasks}
                  calRef={calRef}
                  onChangeMonth={setCalRef}
                  agentLabel={agentLabel}
                  onDayClick={(date) => openModal(date)}
                />
              )}

              {/* ── List view ─────────────────────────────────────────────── */}
              {viewMode === 'list' && (
                state.tasks.length === 0
                  ? <p className="cv-empty">Sin tareas programadas.</p>
                  : (
                    <ul className="cv-list" role="list">
                      {state.tasks.map((task, i) => (
                        <li key={task.trigger_id ?? task.task_id ?? i}>
                          <ConfiguredTaskRow
                            task={task}
                            onToggle={handleToggle}
                            onDelete={handleDelete}
                          />
                        </li>
                      ))}
                    </ul>
                  )
              )}
            </>
          )}
        </section>

        {/* ── Recent runs ─────────────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Ejecuciones recientes">
          <h2 className="cv-section-label">Ejecuciones recientes</h2>
          {state.recentTasks.length === 0
            ? <p className="cv-empty">Sin ejecuciones recientes.</p>
            : (
              <ul className="cv-list" role="list">
                {state.recentTasks.map((task, i) => (
                  <li key={task.task_id ?? i}>
                    <RecentTaskRow task={task} />
                  </li>
                ))}
              </ul>
            )
          }
        </section>
      </div>

      {/* ── Create task modal ─────────────────────────────────────────────── */}
      {modalOpen && (
        <TaskModal
          agents={state.agents}
          presetDate={modalPresetDate}
          onClose={() => setModalOpen(false)}
          onCreate={async (payload) => {
            try {
              await createTask(payload)
              show('Tarea creada — se ejecutará según la programación', 'ok')
              setModalOpen(false)
              reloadTasks()
            } catch (e) {
              show(e instanceof Error ? e.message : 'Error', 'error')
            }
          }}
        />
      )}
    </>
  )
}

// ── Month calendar ────────────────────────────────────────────────────────────

interface MonthCalendarProps {
  tasks: ConfiguredTask[]
  calRef: Date
  onChangeMonth: (d: Date) => void
  agentLabel: (task: ConfiguredTask) => string
  onDayClick: (date: string) => void
}

function MonthCalendar({ tasks, calRef, onChangeMonth, agentLabel, onDayClick }: MonthCalendarProps) {
  const year = calRef.getFullYear()
  const month = calRef.getMonth()
  const todayStr = ymd(new Date())
  const first = new Date(year, month, 1)
  const startDay = new Date(year, month, 1 - ((first.getDay() + 6) % 7))

  const days: Date[] = []
  for (let i = 0; i < 42; i++) {
    days.push(new Date(startDay.getFullYear(), startDay.getMonth(), startDay.getDate() + i))
  }

  const monthLabel = calRef.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })

  return (
    <div className="cal">
      <div className="cal__head">
        <button
          className="cv-icon-btn"
          aria-label="Mes anterior"
          onClick={() => onChangeMonth(new Date(year, month - 1, 1))}
        >
          ‹
        </button>
        <h3 className="cal__title">{monthLabel}</h3>
        <button
          className="cv-icon-btn"
          aria-label="Mes siguiente"
          onClick={() => onChangeMonth(new Date(year, month + 1, 1))}
        >
          ›
        </button>
        <button
          className="cv-btn cv-btn--secondary cv-btn--sm"
          onClick={() => { const n = new Date(); onChangeMonth(new Date(n.getFullYear(), n.getMonth(), 1)) }}
        >
          Hoy
        </button>
      </div>
      <div className="cal__dows" aria-hidden="true">
        {DOW_LABELS.map(d => <div key={d}>{d}</div>)}
      </div>
      <div className="cal__grid">
        {days.map(d => {
          const muted = d.getMonth() !== month
          const isToday = ymd(d) === todayStr
          const chips = tasksForDate(d, tasks)

          return (
            <div
              key={ymd(d)}
              className={[
                'cal__day',
                muted ? 'cal__day--muted' : '',
                isToday ? 'cal__day--today' : '',
              ].filter(Boolean).join(' ')}
              data-date={ymd(d)}
              role="button"
              tabIndex={0}
              aria-label={`${d.getDate()} — Programar tarea`}
              onClick={(e) => {
                if ((e.target as Element).closest('.task-chip')) return
                onDayClick(ymd(d))
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onDayClick(ymd(d))
                }
              }}
            >
              <span className="cal__daynum">{d.getDate()}</span>
              <div className="cal__chips">
                {chips.map((c, i) => {
                  const id = c.task.target_agent_id ?? c.task.agent_id ?? 'default'
                  const hue = agentHue(id)
                  return (
                    <div
                      key={i}
                      className={`task-chip${c.task.enabled === false ? ' task-chip--off' : ''}`}
                      title={c.task.label ?? c.task.name ?? ''}
                    >
                      {c.time && <span className="task-chip__time">{c.time}</span>}
                      <span className="task-chip__name">{c.task.label ?? c.task.name ?? c.task.task_id ?? 'Tarea'}</span>
                      <span
                        className="task-chip__agent"
                        style={{ background: `hsl(${hue} 70% 50% / .18)`, color: `hsl(${hue} 70% 72%)` }}
                      >
                        {agentLabel(c.task)}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Configured task row ───────────────────────────────────────────────────────

interface ConfiguredTaskRowProps {
  task: ConfiguredTask
  onToggle: (task: ConfiguredTask) => void
  onDelete: (task: ConfiguredTask) => void
}

function ConfiguredTaskRow({ task, onToggle, onDelete }: ConfiguredTaskRowProps) {
  const isEnabled = task.enabled !== false
  const sched = task.recurrence_human ?? task.recurrence ?? task.cron ?? task.schedule ?? ''
  const last = task.last_status ? statusMeta(task.last_status) : null
  const nextRun = task.next_run_at
    ? `Próxima: ${relativeTime(task.next_run_at)}`
    : ''

  return (
    <div className={`task-row${!isEnabled ? ' task-row--disabled' : ''}`}>
      <div className="task-row__info">
        <div className="task-row__name">
          {task.label ?? task.title ?? task.name ?? task.task_id ?? 'Tarea'}
          {task.one_shot && <span className="task-meta-chip">Una vez</span>}
        </div>
        {sched && <div className="task-row__schedule">{sched}</div>}
        {nextRun && <div className="task-row__schedule">{nextRun}</div>}
      </div>
      <div className="task-row__actions">
        {last && <span className="task-status-chip">{last.label}</span>}
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm"
          onClick={() => onToggle(task)}
          aria-label={isEnabled ? 'Desactivar tarea' : 'Activar tarea'}
        >
          {isEnabled ? 'Pausar' : 'Activar'}
        </button>
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
          onClick={() => onDelete(task)}
          aria-label="Eliminar tarea"
        >
          ✕
        </button>
      </div>
    </div>
  )
}

// ── Recent task row ───────────────────────────────────────────────────────────

function RecentTaskRow({ task }: { task: RecentTask }) {
  const meta = statusMeta(task.status ?? '')
  const when = task.claimed_at ?? task.enqueued_at ?? task.started_at

  return (
    <div className="recent-task-row">
      <div className="recent-task-row__info">
        <div className="recent-task-row__name">{task.label ?? task.name ?? task.task_id ?? 'Tarea'}</div>
        {when && <div className="recent-task-row__time">{relativeTime(when)}</div>}
      </div>
      <span className="task-status-chip">{meta.label}</span>
    </div>
  )
}

// ── Task creation modal ───────────────────────────────────────────────────────

interface TaskModalProps {
  agents: Agent[]
  presetDate: string | null
  onClose: () => void
  onCreate: (payload: CreateTaskPayload) => void
}

function TaskModal({ agents, presetDate, onClose, onCreate }: TaskModalProps) {
  const [mode, setMode] = useState<'recurrent' | 'once'>(presetDate ? 'once' : 'recurrent')
  const [selectedDays, setSelectedDays] = useState<Set<number>>(new Set())
  const [creating, setCreating] = useState(false)
  const [fieldErrors, setFieldErrors] = useState<{ name?: string; prompt?: string; date?: string; days?: string }>({})
  const nameRef = useRef<HTMLInputElement>(null)
  const promptRef = useRef<HTMLTextAreaElement>(null)
  const timeRef = useRef<HTMLInputElement>(null)
  const timeEndRef = useRef<HTMLInputElement>(null)
  const dateRef = useRef<HTMLInputElement>(null)
  const agentRef = useRef<HTMLSelectElement>(null)
  const riskRef = useRef<HTMLSelectElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    nameRef.current?.focus()
  }, [])

  function toggleDay(day: number) {
    setSelectedDays(prev => {
      const next = new Set(prev)
      if (next.has(day)) next.delete(day); else next.add(day)
      return next
    })
  }

  function toggleAllDays() {
    setSelectedDays(prev => prev.size === 7 ? new Set() : new Set([0, 1, 2, 3, 4, 5, 6]))
  }

  async function handleCreate() {
    const name = nameRef.current?.value.trim() ?? ''
    let prompt = promptRef.current?.value.trim() ?? ''
    const errors: typeof fieldErrors = {}

    if (!name || !prompt) {
      if (!name) errors.name = 'Pon nombre e instrucción'
      if (!prompt) errors.prompt = 'Pon nombre e instrucción'
      setFieldErrors(errors)
      show('Pon nombre e instrucción', 'warn')
      return
    }

    if (mode === 'once') {
      const date = dateRef.current?.value ?? ''
      if (!date) {
        setFieldErrors({ date: 'Elige una fecha' })
        show('Elige una fecha', 'warn')
        return
      }
    } else {
      if (selectedDays.size === 0) {
        setFieldErrors({ days: 'Selecciona al menos un día' })
        show('Selecciona al menos un día', 'warn')
        return
      }
    }
    setFieldErrors({})

    const time = timeRef.current?.value || '09:00'
    const timeEnd = timeEndRef.current?.value
    if (timeEnd) prompt += `\n\n(Ventana de trabajo: ${time}–${timeEnd}.)`

    const days = Array.from(selectedDays)
    const date = dateRef.current?.value ?? ''
    const cron = buildCron({ mode, days, date, time })

    setCreating(true)
    try {
      await onCreate({
        label: name,
        cron,
        instruction: prompt,
        target_agent_id: agentRef.current?.value || undefined,
        risk_ceiling: riskRef.current?.value || 'low',
        one_shot: mode === 'once',
      })
    } finally {
      setCreating(false)
    }
  }

  const customAgents = agents.filter(a => !a.is_default)

  return (
    <div
      className="modal-overlay"
      ref={overlayRef}
      onClick={e => { if (e.target === overlayRef.current) onClose() }}
    >
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-label="Programar una tarea"
      >
        <div className="modal-card__head">
          <h3 className="modal-card__title">Programar una tarea</h3>
          <button className="cv-icon-btn" onClick={onClose} aria-label="Cerrar">✕</button>
        </div>

        <div className="modal-card__body">
          <div className="cv-form-stack">
            <label className="cv-label" htmlFor="tm-name">Nombre</label>
            <input
              id="tm-name"
              ref={nameRef}
              className="cv-input"
              type="text"
              placeholder="Informe diario de ventas"
              autoComplete="off"
              aria-describedby={fieldErrors.name ? 'tm-name-err' : undefined}
              aria-invalid={fieldErrors.name ? true : undefined}
            />
            {fieldErrors.name && (
              <p id="tm-name-err" role="alert" className="office-field-error">{fieldErrors.name}</p>
            )}

            <label className="cv-label" htmlFor="tm-prompt">Instrucción</label>
            <textarea
              id="tm-prompt"
              ref={promptRef}
              className="cv-textarea"
              rows={3}
              placeholder="Qué debe hacer Lumen…"
              aria-describedby={fieldErrors.prompt ? 'tm-prompt-err' : undefined}
              aria-invalid={fieldErrors.prompt ? true : undefined}
            />
            {fieldErrors.prompt && (
              <p id="tm-prompt-err" role="alert" className="office-field-error">{fieldErrors.prompt}</p>
            )}

            <label className="cv-label" htmlFor="tm-mode">Frecuencia</label>
            <select
              id="tm-mode"
              className="cv-input"
              value={mode}
              onChange={e => setMode(e.target.value as 'recurrent' | 'once')}
            >
              <option value="recurrent">Recurrente</option>
              <option value="once">Una vez</option>
            </select>

            {mode === 'recurrent' && (
              <>
                <label className="cv-label">¿Qué días?</label>
                <div
                  className="day-chips"
                  aria-describedby={fieldErrors.days ? 'tm-days-err' : undefined}
                >
                  {DOW_LABELS.map((label, i) => (
                    <button
                      key={i}
                      type="button"
                      className={`day-chip${selectedDays.has(i) ? ' is-on' : ''}`}
                      onClick={() => toggleDay(i)}
                      aria-pressed={selectedDays.has(i)}
                    >
                      {label}
                    </button>
                  ))}
                  <button
                    type="button"
                    className={`day-chip day-chip--all${selectedDays.size === 7 ? ' is-on' : ''}`}
                    onClick={toggleAllDays}
                    aria-pressed={selectedDays.size === 7}
                  >
                    Todos los días
                  </button>
                </div>
                {fieldErrors.days && (
                  <p id="tm-days-err" role="alert" className="office-field-error">{fieldErrors.days}</p>
                )}
              </>
            )}

            {mode === 'once' && (
              <>
                <label className="cv-label" htmlFor="tm-date">¿Qué día?</label>
                <input
                  id="tm-date"
                  ref={dateRef}
                  className="cv-input"
                  type="date"
                  defaultValue={presetDate ?? undefined}
                  aria-describedby={fieldErrors.date ? 'tm-date-err' : undefined}
                  aria-invalid={fieldErrors.date ? true : undefined}
                />
                {fieldErrors.date && (
                  <p id="tm-date-err" role="alert" className="office-field-error">{fieldErrors.date}</p>
                )}
              </>
            )}

            <div className="task-form-grid">
              <div>
                <label className="cv-label" htmlFor="tm-time">¿A qué hora?</label>
                <input id="tm-time" ref={timeRef} className="cv-input" type="time" defaultValue="09:00" />
              </div>
              <div>
                <label className="cv-label" htmlFor="tm-time-end">Hasta (opcional)</label>
                <input id="tm-time-end" ref={timeEndRef} className="cv-input" type="time" />
              </div>
              <div>
                <label className="cv-label" htmlFor="tm-agent">Agente</label>
                <select id="tm-agent" ref={agentRef} className="cv-input">
                  <option value="">Agente principal (por defecto)</option>
                  {customAgents.map(a => (
                    <option key={a.id} value={a.id}>{a.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="cv-label" htmlFor="tm-risk">Riesgo</label>
                <select id="tm-risk" ref={riskRef} className="cv-input" defaultValue="low">
                  <option value="low">Bajo</option>
                  <option value="high">Alto</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div className="modal-card__actions">
          <button className="cv-btn cv-btn--ghost cv-btn--sm" onClick={onClose}>Cancelar</button>
          <button
            className="cv-btn cv-btn--primary cv-btn--sm"
            onClick={handleCreate}
            disabled={creating}
          >
            {creating ? 'Creando…' : 'Crear tarea'}
          </button>
        </div>
      </div>
    </div>
  )
}

