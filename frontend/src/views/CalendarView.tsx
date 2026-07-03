import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { Calendar, ChevronLeft, ChevronRight, Trash2, X } from 'lucide-react'
import { useT, useLocale } from '../lib/i18n'
import { listConfiguredTasks, listRecentTasks, createTask, deleteTask, toggleTask, listAgents, ApiError } from '../api/client'
import type { ConfiguredTask, RecentTask, Agent, CreateTaskPayload } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { Drawer } from '../components/ui/Drawer'
import {
  AnimatePresence,
  AnimatedListItem,
  Stagger,
  StaggerItem,
  HoverRow,
  motion,
  SPRING,
} from '../components/ui/motion'
import styles from './CalendarView.module.css'

// ── Cron parsing (mirrors tasks-view.js exactly) ──────────────────────────────

const DOW_KEYS = ['cal.dow.mon', 'cal.dow.tue', 'cal.dow.wed', 'cal.dow.thu', 'cal.dow.fri', 'cal.dow.sat', 'cal.dow.sun'] as const

function dowLabels(t: ReturnType<typeof useT>): string[] {
  return DOW_KEYS.map(k => t(k))
}

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
    // Emit standard 5-field cron "min hour dd mo *" — the backend uses
    // next_run_at for the actual scheduling date; the year is NOT encoded here.
    // one_shot:true in the payload tells the backend not to repeat.
    const [, mo, dd] = (date || '').split('-')
    return `${min} ${hour} ${parseInt(dd, 10)} ${parseInt(mo, 10)} *`
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

function relativeTime(iso: string | undefined, t: ReturnType<typeof useT>): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return t('cal.time.now')
  if (mins < 60) return t('cal.time.mins_ago').replace('{n}', String(mins))
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return t('cal.time.hours_ago').replace('{n}', String(hrs))
  return t('cal.time.days_ago').replace('{n}', String(Math.floor(hrs / 24)))
}

type StatusKind = 'completed' | 'running' | 'failed' | 'default'

function statusMeta(status: string, t: ReturnType<typeof useT>): { label: string; kind: StatusKind } {
  const s = String(status).toLowerCase()
  if (s === 'completed' || s === 'done' || s === 'success') return { label: t('cal.status.completed'), kind: 'completed' }
  if (s === 'in_progress' || s === 'running' || s === 'claimed') return { label: t('cal.status.running'), kind: 'running' }
  if (s === 'failed' || s === 'error') return { label: t('cal.status.failed'), kind: 'failed' }
  return { label: status, kind: 'default' }
}

// ── State types ───────────────────────────────────────────────────────────────

type ViewMode = 'board' | 'list' | 'runs'

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

// ── Recurrence label ──────────────────────────────────────────────────────────

function recurrenceLabel(task: ConfiguredTask, t: ReturnType<typeof useT>): string {
  if (task.recurrence_human) return task.recurrence_human
  if (task.one_shot) return t('cal.once')
  const cron = taskCron(task)
  if (!cron) return ''
  const { days, time, daily, valid } = parseCron(cron)
  if (!valid) return cron
  const timeStr = time ? t('cal.at_time').replace('{time}', time) : ''
  if (daily) return `${t('cal.every_day')}${timeStr}`
  const dayNames = dowLabels(t)
  const dayList = Array.from(days).sort((a, b) => a - b).map(d => dayNames[d]).join(', ')
  return `${dayList}${timeStr}`
}

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

// ── Loading skeleton ──────────────────────────────────────────────────────────

function CalendarSkeleton({ mode }: { mode: ViewMode }) {
  const t = useT()
  if (mode === 'board') {
    return (
      <div className={styles.skeletonSection} aria-busy="true" aria-label={t('cal.loading.calendar_aria')}>
        <div className={styles.skeletonCalHead} />
        <div className={styles.skeletonCalGrid}>
          {Array.from({ length: 42 }).map((_, i) => (
            <div
              key={i}
              className={styles.skeletonCalCell}
              style={{ animationDelay: `${(i % 7) * 30}ms` }}
            />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className={styles.skeletonSection} aria-busy="true" aria-label={t('cal.loading.tasks_aria')}>
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className={styles.skeletonRow}
          style={{ animationDelay: `${i * 50}ms` }}
        />
      ))}
    </div>
  )
}

// ── Status chip ───────────────────────────────────────────────────────────────

function StatusChip({ kind, label }: { kind: StatusKind; label: string }) {
  const cls = {
    completed: styles.statusChipCompleted,
    running: styles.statusChipRunning,
    failed: styles.statusChipFailed,
    default: styles.statusChipDefault,
  }[kind]

  return (
    <span className={`${styles.statusChip} ${cls}`}>
      <span className={styles.statusDot} aria-hidden="true" />
      {label}
    </span>
  )
}

export default function CalendarView() {
  const t = useT()
  const [state, dispatch] = useReducer(calReducer, {
    tasks: [], recentTasks: [], agents: [], loading: true, error: null,
  })
  const [viewMode, setViewMode] = useState<ViewMode>('board')
  const [calRef, setCalRef] = useState<Date>(() => { const n = new Date(); return new Date(n.getFullYear(), n.getMonth(), 1) })
  const [modalOpen, setModalOpen] = useState(false)
  const [modalPresetDate, setModalPresetDate] = useState<string | null>(null)
  const [detailTask, setDetailTask] = useState<ConfiguredTask | null>(null)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  const agentsById = Object.fromEntries(state.agents.map(a => [a.id, a]))

  function agentLabel(task: ConfiguredTask): string {
    const id = task.target_agent_id ?? task.agent_id ?? ''
    if (!id) return t('cal.default_agent')
    const a = agentsById[id]
    if (a?.is_default) return t('cal.default_agent')
    return a?.name ?? t('cal.default_agent')
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
      dispatch({ type: 'FAILED', error: e instanceof ApiError ? e.message : t('cal.err.load') })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
      show(task.enabled !== false ? t('cal.toast.paused') : t('cal.toast.activated'), 'ok')
      reloadTasks()
    } catch (e) { show(e instanceof Error ? e.message : t('cal.err.generic'), 'error') }
  }

  async function handleDelete(task: ConfiguredTask) {
    const id = task.trigger_id ?? task.task_id ?? task.id ?? ''
    const name = task.label ?? task.name ?? id
    const ok = await confirm({
      title: t('cal.delete.confirm.title').replace('{name}', name),
      description: t('cal.delete.confirm.desc'),
      confirmLabel: t('cal.delete.confirm.label'),
      variant: 'danger',
    })
    if (!ok) return
    try {
      await deleteTask(id)
      show(t('cal.toast.deleted'), 'ok')
      reloadTasks()
    } catch (e) { show(e instanceof Error ? e.message : t('cal.err.generic'), 'error') }
  }

  return (
    <>
      {ConfirmDialogNode}
      <PageHeader
        title={t('view.programadas')}
        subtitle={t('cal.subtitle')}
        actions={
          <Button variant="primary" size="sm" onClick={() => openModal()}>
            {t('cal.new_task')}
          </Button>
        }
      />

      <div className="view-body cv-view-body">
        <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>
          <StaggerItem>
            <section className="cv-section" aria-label={t('view.programadas')}>
              {/* Section header with view switcher */}
              <div className={styles.sectionHead}>
                <h2 className={styles.sectionLabel}>{t('cal.section.label')}</h2>
                <div className={styles.sectionHeadRight}>
                  <div
                    className={styles.segToggle}
                    role="tablist"
                    aria-label={t('cal.view_mode.aria')}
                  >
                    {(
                      [
                        { key: 'board', label: t('cal.tab.board') },
                        { key: 'list',  label: t('cal.tab.list') },
                        { key: 'runs',  label: t('cal.tab.runs') },
                      ] as const
                    ).map(tab => (
                      <button
                        key={tab.key}
                        className={`${styles.segBtn}${viewMode === tab.key ? ` ${styles.segBtnActive}` : ''}`}
                        role="tab"
                        aria-selected={viewMode === tab.key}
                        aria-controls={`tab-panel-${tab.key}`}
                        id={`tab-${tab.key}`}
                        onClick={() => setViewMode(tab.key)}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* Loading state — shape-mirroring skeletons */}
              {state.loading && <CalendarSkeleton mode={viewMode} />}

              {/* Error state — inline banner with retry */}
              {!state.loading && state.error && (
                <div className={styles.errorBanner} role="alert">
                  <p className={styles.errorMessage}>{state.error}</p>
                  <Button variant="secondary" size="sm" onClick={loadAll}>{t('cal.retry')}</Button>
                </div>
              )}

              {/* Content — animated tab panels */}
              {!state.loading && !state.error && (
                <AnimatePresence mode="wait" initial={false}>
                  {viewMode === 'board' && (
                    <motion.div
                      key="calendar"
                      id="tab-panel-board"
                      role="tabpanel"
                      aria-labelledby="tab-board"
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      transition={SPRING}
                    >
                      <MonthCalendar
                        tasks={state.tasks}
                        calRef={calRef}
                        onChangeMonth={setCalRef}
                        agentLabel={agentLabel}
                        onDayClick={(date) => openModal(date)}
                        onTaskClick={setDetailTask}
                      />
                    </motion.div>
                  )}

                  {viewMode === 'list' && (
                    <motion.div
                      key="list"
                      id="tab-panel-list"
                      role="tabpanel"
                      aria-labelledby="tab-list"
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      transition={SPRING}
                    >
                      {state.tasks.length === 0 ? (
                        <EmptyState
                          icon={<Calendar size={36} />}
                          title={t('cal.empty.title')}
                          description={t('cal.empty.desc')}
                          action={
                            <Button variant="primary" size="sm" onClick={() => openModal()}>
                              {t('cal.empty.cta')}
                            </Button>
                          }
                        />
                      ) : (
                        <ul className="cv-list" role="list">
                          <AnimatePresence initial={false}>
                            {state.tasks.map(task => (
                              <AnimatedListItem key={task.trigger_id ?? task.task_id ?? task.id}>
                                <ConfiguredTaskRow
                                  task={task}
                                  onViewDetail={setDetailTask}
                                  onToggle={handleToggle}
                                  onDelete={handleDelete}
                                />
                              </AnimatedListItem>
                            ))}
                          </AnimatePresence>
                        </ul>
                      )}
                    </motion.div>
                  )}

                  {viewMode === 'runs' && (
                    <motion.div
                      key="runs"
                      id="tab-panel-runs"
                      role="tabpanel"
                      aria-labelledby="tab-runs"
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      transition={SPRING}
                    >
                      {state.recentTasks.length === 0 ? (
                        <EmptyState
                          icon={<Calendar size={28} />}
                          title={t('cal.runs.empty.title')}
                          description={t('cal.runs.empty.desc')}
                        />
                      ) : (
                        <ul className="cv-list" role="list">
                          <AnimatePresence initial={false}>
                            {state.recentTasks.map(task => (
                              <AnimatedListItem key={task.task_id}>
                                <RecentTaskRow task={task} />
                              </AnimatedListItem>
                            ))}
                          </AnimatePresence>
                        </ul>
                      )}
                    </motion.div>
                  )}
                </AnimatePresence>
              )}
            </section>
          </StaggerItem>
        </Stagger>
      </div>

      {/* ── Create task modal ─────────────────────────────────────────────────── */}
      {modalOpen && (
        <TaskModal
          agents={state.agents}
          presetDate={modalPresetDate}
          onClose={() => setModalOpen(false)}
          onCreate={async (payload) => {
            try {
              await createTask(payload)
              show(t('cal.toast.created'), 'ok')
              setModalOpen(false)
              reloadTasks()
            } catch (e) {
              show(e instanceof Error ? e.message : t('cal.err.generic'), 'error')
            }
          }}
        />
      )}

      {/* ── Task detail drawer */}
      <TaskDetailDrawer
        task={detailTask}
        agentLabel={agentLabel}
        onClose={() => setDetailTask(null)}
      />
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
  onTaskClick: (task: ConfiguredTask) => void
}

function MonthCalendar({ tasks, calRef, onChangeMonth, agentLabel, onDayClick, onTaskClick }: MonthCalendarProps) {
  const t = useT()
  const { locale } = useLocale()
  const year = calRef.getFullYear()
  const month = calRef.getMonth()
  const todayStr = ymd(new Date())
  const first = new Date(year, month, 1)
  const startDay = new Date(year, month, 1 - ((first.getDay() + 6) % 7))

  const days: Date[] = []
  for (let i = 0; i < 42; i++) {
    days.push(new Date(startDay.getFullYear(), startDay.getMonth(), startDay.getDate() + i))
  }

  const monthLabel = calRef.toLocaleDateString(locale === 'en' ? 'en-US' : 'es-ES', { month: 'long', year: 'numeric' })
  const weekDays = dowLabels(t)

  return (
    <div className="cal">
      <div className={styles.calHead}>
        <button
          className={styles.calIconBtn}
          aria-label={t('cal.month.prev')}
          onClick={() => onChangeMonth(new Date(year, month - 1, 1))}
        >
          <ChevronLeft size={14} aria-hidden="true" />
        </button>
        <h3 className={styles.calTitle}>{monthLabel}</h3>
        <button
          className={styles.calIconBtn}
          aria-label={t('cal.month.next')}
          onClick={() => onChangeMonth(new Date(year, month + 1, 1))}
        >
          <ChevronRight size={14} aria-hidden="true" />
        </button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => { const n = new Date(); onChangeMonth(new Date(n.getFullYear(), n.getMonth(), 1)) }}
        >
          {t('cal.today')}
        </Button>
      </div>

      <div className={styles.calDows} aria-hidden="true">
        {weekDays.map(d => (
          <div key={d} className={styles.calDow}>{d}</div>
        ))}
      </div>

      <div className={styles.calGrid}>
        {days.map(d => {
          const muted = d.getMonth() !== month
          const isToday = ymd(d) === todayStr
          const chips = tasksForDate(d, tasks)

          return (
            <div
              key={ymd(d)}
              className={[
                styles.calDay,
                muted ? styles.calDayMuted : '',
              ].filter(Boolean).join(' ')}
              data-date={ymd(d)}
              role="button"
              tabIndex={0}
              aria-label={t('cal.day.aria').replace('{day}', String(d.getDate()))}
              onClick={(e) => {
                if ((e.target as Element).closest(`.${styles.taskChip}`)) return
                onDayClick(ymd(d))
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onDayClick(ymd(d))
                }
              }}
            >
              <span
                className={`${styles.calDayNum}${isToday ? ` ${styles.calDayNumToday}` : ''}`}
              >
                {d.getDate()}
              </span>
              <div className={styles.calChips}>
                {chips.map((c, i) => {
                  const id = c.task.target_agent_id ?? c.task.agent_id ?? 'default'
                  const hue = agentHue(id)
                  return (
                    <div
                      key={i}
                      className={`${styles.taskChip}${c.task.enabled === false ? ` ${styles.taskChipOff}` : ''}`}
                      title={t('cal.chip.title').replace('{name}', c.task.label ?? c.task.name ?? '')}
                      role="button"
                      tabIndex={0}
                      aria-label={t('cal.chip.aria').replace('{name}', c.task.label ?? c.task.name ?? t('cal.task_fallback'))}
                      onClick={(e) => { e.stopPropagation(); onTaskClick(c.task) }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault(); e.stopPropagation(); onTaskClick(c.task)
                        }
                      }}
                    >
                      {c.time && <span className={styles.taskChipTime}>{c.time}</span>}
                      <span className={styles.taskChipName}>{c.task.label ?? c.task.name ?? c.task.task_id ?? t('cal.task_fallback')}</span>
                      <span
                        className={styles.taskChipAgent}
                        style={{ background: `hsl(${hue} 70% 50% / .16)`, color: `hsl(${hue} 65% 70%)` }}
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
  onViewDetail: (task: ConfiguredTask) => void
  onToggle: (task: ConfiguredTask) => void
  onDelete: (task: ConfiguredTask) => void
}

function ConfiguredTaskRow({ task, onViewDetail, onToggle, onDelete }: ConfiguredTaskRowProps) {
  const t = useT()
  const isEnabled = task.enabled !== false
  const recurrence = recurrenceLabel(task, t)
  const last = task.last_status ? statusMeta(task.last_status, t) : null
  const nextRunLabel = task.next_run_at ? relativeTime(task.next_run_at, t) : null
  const taskName = task.label ?? task.title ?? task.name ?? task.task_id ?? t('cal.task_fallback')

  return (
    <HoverRow
      className={`${styles.taskRow}${!isEnabled ? ` ${styles.taskRowDisabled}` : ''}`}
      role="button"
      tabIndex={0}
      aria-label={t('cal.chip.aria').replace('{name}', taskName)}
      onClick={() => onViewDetail(task)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onViewDetail(task) } }}
    >
      <div className={styles.taskInfo}>
        <div className={styles.taskName}>
          {taskName}
          {task.one_shot && <span className={styles.oneShotBadge}>{t('cal.badge.once')}</span>}
          {!isEnabled && <span className={styles.pausedBadge}>{t('cal.badge.paused')}</span>}
        </div>
        {recurrence && <div className={styles.taskSchedule}>{recurrence}</div>}
        {nextRunLabel && (
          <div className={styles.taskNextRun}>{t('cal.next_run').replace('{when}', nextRunLabel)}</div>
        )}
      </div>

      <div className={styles.taskActions} onClick={(e) => e.stopPropagation()}>
        {last && <StatusChip kind={last.kind} label={last.label} />}
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm"
          onClick={() => onViewDetail(task)}
          aria-label={t('cal.view.aria')}
        >
          {t('cal.view')}
        </button>
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm"
          onClick={() => onToggle(task)}
          aria-label={isEnabled ? t('cal.pause.aria') : t('cal.activate.aria')}
        >
          {isEnabled ? t('cal.pause') : t('cal.activate')}
        </button>
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
          onClick={() => onDelete(task)}
          aria-label={t('cal.delete.aria')}
        >
          <Trash2 size={13} aria-hidden="true" />
        </button>
      </div>
    </HoverRow>
  )
}

// ── Recent task row ───────────────────────────────────────────────────────────

function RecentTaskRow({ task }: { task: RecentTask }) {
  const t = useT()
  const meta = statusMeta(task.status ?? '', t)
  const when = task.claimed_at ?? task.enqueued_at ?? task.started_at

  return (
    <HoverRow className={styles.recentRow}>
      <div className={styles.recentInfo}>
        <div className={styles.recentName}>{task.label ?? task.name ?? task.task_id ?? t('cal.task_fallback')}</div>
        {when && <div className={styles.recentTime}>{relativeTime(when, t)}</div>}
      </div>
      <StatusChip kind={meta.kind} label={meta.label} />
    </HoverRow>
  )
}

// ── Task detail drawer ────────────────────────────────────────────────────────

interface TaskDetailDrawerProps {
  task: ConfiguredTask | null
  agentLabel: (task: ConfiguredTask) => string
  onClose: () => void
}

function TaskDetailDrawer({ task, agentLabel, onClose }: TaskDetailDrawerProps) {
  const t = useT()
  const { locale } = useLocale()
  const recurrence = task ? recurrenceLabel(task, t) : ''
  const nextRun = task?.next_run_at
    ? new Date(task.next_run_at).toLocaleString(locale === 'en' ? 'en-US' : 'es-ES', { dateStyle: 'medium', timeStyle: 'short' })
    : null
  const riskLabel = task?.risk_ceiling === 'high' ? t('cal.risk.high') : t('cal.risk.low')

  return (
    <Drawer open={task !== null} title={task?.label ?? task?.title ?? task?.name ?? t('cal.task_fallback')} onClose={onClose}>
      {task && (
        <div className={styles.drawerBody}>
          {/* Badges row */}
          {(task.one_shot || task.enabled === false) && (
            <div className={styles.drawerBadgeRow}>
              {task.one_shot && <span className={styles.oneShotBadge}>{t('cal.once')}</span>}
              {task.enabled === false && <span className={styles.pausedBadge}>{t('cal.badge.paused')}</span>}
            </div>
          )}

          {/* Instruction */}
          {task.instruction && (
            <div className={styles.drawerInstruction}>
              <p className={styles.drawerFieldLabel}>{t('cal.field.instruction')}</p>
              <p className={styles.drawerFieldValue}>{task.instruction}</p>
            </div>
          )}

          {/* Metadata grid */}
          <div className={styles.drawerMeta}>
            <div className={styles.drawerMetaField}>
              <p className={styles.drawerFieldLabel}>{t('cal.field.agent')}</p>
              <p className={styles.drawerMetaValue}>{agentLabel(task)}</p>
            </div>
            {recurrence && (
              <div className={styles.drawerMetaField}>
                <p className={styles.drawerFieldLabel}>{t('cal.field.recurrence')}</p>
                <p className={styles.drawerMetaValue}>{recurrence}</p>
              </div>
            )}
            {task.risk_ceiling && (
              <div className={styles.drawerMetaField}>
                <p className={styles.drawerFieldLabel}>{t('cal.field.risk')}</p>
                <p className={styles.drawerMetaValue}>{riskLabel}</p>
              </div>
            )}
            {nextRun && (
              <div className={styles.drawerMetaField}>
                <p className={styles.drawerFieldLabel}>{t('cal.field.next_run')}</p>
                <p className={styles.drawerMetaValueMono}>{nextRun}</p>
              </div>
            )}
          </div>

          <div className={styles.drawerActions}>
            <Button variant="ghost" size="sm" onClick={onClose}>{t('cal.close')}</Button>
          </div>
        </div>
      )}
    </Drawer>
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
  const t = useT()
  const initialMode: 'recurrent' | 'once' = presetDate ? 'once' : 'recurrent'

  const [mode, setMode] = useState<'recurrent' | 'once'>(initialMode)
  const [selectedDays, setSelectedDays] = useState<Set<number>>(new Set<number>())
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
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
      if (!name) errors.name = t('cal.err.name')
      if (!prompt) errors.prompt = t('cal.err.prompt')
      setFieldErrors(errors)
      show(t('cal.warn.required'), 'warn')
      return
    }

    if (mode === 'once') {
      const date = dateRef.current?.value ?? ''
      if (!date) {
        setFieldErrors({ date: t('cal.err.date') })
        show(t('cal.err.date'), 'warn')
        return
      }
    } else {
      if (selectedDays.size === 0) {
        setFieldErrors({ days: t('cal.err.days') })
        show(t('cal.err.days'), 'warn')
        return
      }
    }
    setFieldErrors({})

    const time = timeRef.current?.value || '09:00'
    const timeEnd = timeEndRef.current?.value
    if (timeEnd) prompt += t('cal.window_suffix').replace('{start}', time).replace('{end}', timeEnd)

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
    <motion.div
      className="modal-overlay"
      ref={overlayRef}
      onClick={e => { if (e.target === overlayRef.current) onClose() }}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
    >
      <motion.div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-label={t('cal.modal.title')}
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: 16 }}
        transition={SPRING}
      >
        <div className={styles.modalHead}>
          <h3 className={styles.modalTitle}>{t('cal.modal.title')}</h3>
          <button className={styles.modalCloseBtn} onClick={onClose} aria-label={t('cal.modal.close.aria')}>
            <X size={14} aria-hidden="true" />
          </button>
        </div>

        <div className={styles.modalBody}>
          <div className={styles.formStack}>
            <label className={styles.formLabel} htmlFor="tm-name">{t('cal.form.name')}</label>
            <input
              id="tm-name"
              ref={nameRef}
              className={styles.formInput}
              type="text"
              placeholder={t('cal.form.name.placeholder')}
              autoComplete="off"
              aria-describedby={fieldErrors.name ? 'tm-name-err' : undefined}
              aria-invalid={fieldErrors.name ? true : undefined}
            />
            {fieldErrors.name && (
              <p id="tm-name-err" role="alert" className={styles.formFieldError}>{fieldErrors.name}</p>
            )}

            <label className={styles.formLabel} htmlFor="tm-prompt">{t('cal.form.instruction')}</label>
            <textarea
              id="tm-prompt"
              ref={promptRef}
              className={`${styles.formInput} ${styles.formTextarea}`}
              rows={3}
              placeholder={t('cal.form.instruction.placeholder')}
              aria-describedby={fieldErrors.prompt ? 'tm-prompt-err' : undefined}
              aria-invalid={fieldErrors.prompt ? true : undefined}
            />
            {fieldErrors.prompt && (
              <p id="tm-prompt-err" role="alert" className={styles.formFieldError}>{fieldErrors.prompt}</p>
            )}

            <label className={styles.formLabel} htmlFor="tm-mode">{t('cal.form.frequency')}</label>
            <select
              id="tm-mode"
              className={styles.formInput}
              value={mode}
              onChange={e => setMode(e.target.value as 'recurrent' | 'once')}
            >
              <option value="recurrent">{t('cal.form.frequency.recurrent')}</option>
              <option value="once">{t('cal.form.frequency.once')}</option>
            </select>

            {mode === 'recurrent' && (
              <>
                <label className={styles.formLabel}>{t('cal.form.days')}</label>
                <div
                  className={styles.dayChips}
                  aria-describedby={fieldErrors.days ? 'tm-days-err' : undefined}
                >
                  {dowLabels(t).map((label, i) => (
                    <button
                      key={i}
                      type="button"
                      className={`${styles.dayChip}${selectedDays.has(i) ? ` ${styles.dayChipOn}` : ''}`}
                      onClick={() => toggleDay(i)}
                      aria-pressed={selectedDays.has(i)}
                    >
                      {label}
                    </button>
                  ))}
                  <button
                    type="button"
                    className={`${styles.dayChip} ${styles.dayChipAll}${selectedDays.size === 7 ? ` ${styles.dayChipOn}` : ''}`}
                    onClick={toggleAllDays}
                    aria-pressed={selectedDays.size === 7}
                  >
                    {t('cal.every_day')}
                  </button>
                </div>
                {fieldErrors.days && (
                  <p id="tm-days-err" role="alert" className={styles.formFieldError}>{fieldErrors.days}</p>
                )}
              </>
            )}

            {mode === 'once' && (
              <>
                <label className={styles.formLabel} htmlFor="tm-date">{t('cal.form.date')}</label>
                <input
                  id="tm-date"
                  ref={dateRef}
                  className={styles.formInput}
                  type="date"
                  defaultValue={presetDate ?? undefined}
                  aria-describedby={fieldErrors.date ? 'tm-date-err' : undefined}
                  aria-invalid={fieldErrors.date ? true : undefined}
                />
                {fieldErrors.date && (
                  <p id="tm-date-err" role="alert" className={styles.formFieldError}>{fieldErrors.date}</p>
                )}
              </>
            )}

            <div className={styles.formGrid}>
              <div>
                <label className={styles.formLabel} htmlFor="tm-time">{t('cal.form.time')}</label>
                <input id="tm-time" ref={timeRef} className={styles.formInput} type="time" defaultValue="09:00" />
              </div>
              <div>
                <label className={styles.formLabel} htmlFor="tm-time-end">{t('cal.form.time_end')}</label>
                <input id="tm-time-end" ref={timeEndRef} className={styles.formInput} type="time" />
              </div>
              <div>
                <label className={styles.formLabel} htmlFor="tm-agent">{t('cal.field.agent')}</label>
                <select id="tm-agent" ref={agentRef} className={styles.formInput}>
                  <option value="">{t('cal.form.agent.default')}</option>
                  {customAgents.map(a => (
                    <option key={a.id} value={a.id}>{a.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className={styles.formLabel} htmlFor="tm-risk">{t('cal.field.risk')}</label>
                <select id="tm-risk" ref={riskRef} className={styles.formInput} defaultValue="low">
                  <option value="low">{t('cal.risk.low')}</option>
                  <option value="high">{t('cal.risk.high')}</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div className={styles.modalActions}>
          <Button variant="ghost" size="sm" onClick={onClose}>{t('cal.cancel')}</Button>
          <Button variant="primary" size="sm" onClick={handleCreate} loading={creating}>
            {t('cal.create')}
          </Button>
        </div>
      </motion.div>
    </motion.div>
  )
}
