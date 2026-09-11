import { useEffect, useRef, useState } from 'react'
import { useNavigate, useOutletContext, useSearchParams } from 'react-router-dom'
import { CheckCheck, ChevronRight, Circle, Clock3, RefreshCw, Search } from 'lucide-react'
import { getTaskDashboard, getTaskInbox } from '../api/client'
import type { TaskDashboardItem, InboundDelegation } from '../api/types'
import type { ChatOutletContext } from '../components/Layout'
import { usePendingApprovals } from '../hooks/usePendingApprovals'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import ApprovalCard from '../components/ApprovalCard'
import InboundDelegationCard from '../components/InboundDelegationCard'
import CalendarView from './CalendarView'
import styles from './TasksView.module.css'

const statuses: Record<string, string> = { pending: 'En cola', in_progress: 'En curso', completed: 'Completada', failed: 'Fallida', pending_approval: 'Necesita aprobación', rejected: 'Rechazada', cancelled: 'Cancelada' }
type Filter = 'all' | 'active' | 'attention' | 'completed'
const filters: [Filter, string][] = [['all', 'Todas'], ['active', 'En curso'], ['attention', 'Necesitan atención'], ['completed', 'Completadas']]
const needsAttention = (status: string) => ['failed', 'pending_approval', 'rejected'].includes(status)
const date = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('es-ES', { dateStyle: 'medium', timeStyle: 'short' }) : 'Sin fecha confirmada'
const optionalText = (value: unknown) => value == null || typeof value === 'string'
function validTask(task: TaskDashboardItem) {
  return task && typeof task.task_id === 'string' && task.task_id.length > 0
    && typeof task.label === 'string' && Object.prototype.hasOwnProperty.call(statuses, task.status)
    && ['local', 'enterprise'].includes(task.source)
    && [task.requested_by, task.created_at, task.updated_at, task.conversation_id, task.result].every(optionalText)
    && (task.approval_ids === undefined || Array.isArray(task.approval_ids) && task.approval_ids.every(id => typeof id === 'string'))
}

export default function TasksView() {
  const [params, setParams] = useSearchParams()
  const scheduled = params.get('tab') === 'programadas'
  return <div className={styles.page}>
    <PageHeader title="Tareas" subtitle="Tu trabajo y los encargos de Enterprise, en un solo lugar." />
    <nav className={styles.tabs} aria-label="Vistas de tareas">
      <button aria-current={!scheduled ? 'page' : undefined} onClick={() => setParams({})}>Actividad y encargos</button>
      <button aria-current={scheduled ? 'page' : undefined} onClick={() => setParams({ tab: 'programadas' })}>Programadas</button>
    </nav>
    {scheduled ? <CalendarView /> : <TaskActivity />}
  </div>
}

function TaskActivity() {
  const navigate = useNavigate()
  const chat = useOutletContext<ChatOutletContext>()
  const approvals = usePendingApprovals(6000)
  const [data, setData] = useState<{ tasks: TaskDashboardItem[]; inbox: InboundDelegation[]; hasMore: boolean } | null>(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [chatError, setChatError] = useState(false)
  const [opening, setOpening] = useState(false)
  const alive = useRef(true)
  const openingRef = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    let mounted = true
    let inFlight = false
    const load = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const [dashboard, inbox] = await Promise.all([getTaskDashboard(), getTaskInbox()])
        if (dashboard.available !== true || !Array.isArray(dashboard.tasks) || !Array.isArray(inbox)
          || typeof dashboard.has_more !== 'boolean'
          || !dashboard.tasks.every(validTask)
          || new Set(dashboard.tasks.map(task => task.task_id)).size !== dashboard.tasks.length
          || inbox.some(item => !item || typeof item.message_id !== 'string' || !item.message_id || typeof item.body !== 'string' || typeof item.from_employee_id !== 'string')) throw new Error('invalid_dashboard')
        if (mounted) { setData({ tasks: dashboard.tasks, inbox, hasMore: dashboard.has_more }); setError(false) }
      } catch { if (mounted) setError(true) }
      finally { inFlight = false; if (mounted) setLoading(false) }
    }
    setLoading(true)
    void load()
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') void load() }, 10000)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [revision])
  const refresh = () => { setRevision(value => value + 1); approvals.refresh() }
  const tasks = data?.tasks ?? []
  const selected = tasks.find(task => task.task_id === selectedId)
  const filtered = tasks.filter(task => (filter === 'all' || filter === 'active' && ['pending', 'in_progress'].includes(task.status)
    || filter === 'attention' && needsAttention(task.status) || filter === 'completed' && task.status === 'completed')
    && `${task.label} ${task.requested_by ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const pending = selected ? approvals.approvals.filter(item => selected.approval_ids?.includes(item.proposal_id)) : approvals.approvals
  async function openConversation() {
    if (!selected?.conversation_id || openingRef.current || error || loading) return
    openingRef.current = true; setOpening(true); setChatError(false)
    try { await chat.loadConversation(selected.conversation_id); if (alive.current) navigate('/chat') }
    catch { if (alive.current) setChatError(true) }
    finally { openingRef.current = false; if (alive.current) setOpening(false) }
  }
  return <>
    <div className={styles.toolbar}>
      <label className={styles.search}><Search size={15} aria-hidden /><span className={styles.srOnly}>Buscar tareas</span><input placeholder="Buscar tareas o remitente…" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <Button variant="ghost" size="sm" disabled={loading} onClick={refresh} aria-label="Actualizar tareas"><RefreshCw size={15} aria-hidden /></Button>
    </div>
    {error && <div className={styles.warning} role="alert"><strong>No se pudo consultar el cuadro de tareas.</strong><p>{data ? 'Se conservan los últimos datos. Las acciones están bloqueadas hasta actualizar.' : 'La conexión con el servicio de tareas aún no está disponible. Esto no significa que no tengas encargos.'}</p></div>}
    {loading && !data && <p className={styles.empty} role="status">Consultando tareas y encargos…</p>}
    {data && <>
      {data.inbox.length > 0 && <section className={styles.inbox} aria-label="Encargos recibidos"><h2>Encargos recibidos <span>{data.inbox.length}</span></h2><p>Aceptar incorpora el encargo al trabajo de tu agente. Las acciones sensibles seguirán necesitando su aprobación.</p><fieldset disabled={error || loading} className={styles.inboxItems}>{data.inbox.map(item => <InboundDelegationCard key={item.message_id} delegation={item} onResolved={refresh} />)}</fieldset></section>}
      <div className={styles.filters} aria-label="Filtrar tareas">{filters.map(([key, label]) => <button key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>)}<span>{tasks.length} tareas recientes{data.hasMore ? ' · hay más anteriores' : ''}</span></div>
      <div className={styles.workspace} data-detail={!!selected}>
        <section className={styles.list} aria-label="Lista de tareas">
          {!filtered.length && <div className={styles.empty}><CheckCheck size={24} aria-hidden /><p>{tasks.length ? 'No hay tareas que coincidan con este filtro.' : 'Todavía no hay tareas registradas.'}</p></div>}
          {filtered.map(task => <button key={task.task_id} className={styles.row} aria-pressed={selectedId === task.task_id} disabled={opening} onClick={() => { setSelectedId(task.task_id); setChatError(false) }}>
            <Circle size={13} className={styles.state} data-status={task.status} aria-hidden />
            <span className={styles.taskText}><strong>{task.label}</strong><span>{task.source === 'enterprise' ? 'Enterprise' : 'Local'}{task.requested_by ? ` · ${task.requested_by}` : ''}</span></span>
            <span className={styles.status}>{statuses[task.status]}</span><ChevronRight size={14} aria-hidden />
          </button>)}
        </section>
        {selected && <aside className={styles.detail} aria-label="Detalle de tarea"><div className={styles.detailHead}><span>{statuses[selected.status]}</span><button onClick={() => setSelectedId(null)} disabled={opening} aria-label="Cerrar detalle">Cerrar</button></div><h2>{selected.label}</h2><dl><div><dt>Origen</dt><dd>{selected.source === 'enterprise' ? 'Enterprise' : 'Esta instancia'}</dd></div>{selected.requested_by && <div><dt>Encargada por</dt><dd>{selected.requested_by}</dd></div>}<div><dt>Creada</dt><dd>{date(selected.created_at)}</dd></div><div><dt>Última actualización</dt><dd>{date(selected.updated_at)}</dd></div></dl>
          <h3>Resultado</h3><div className={styles.result}>{selected.result || (selected.status === 'completed' ? 'La ejecución figura completada, pero todavía no hay un resultado disponible aquí.' : 'El resultado aparecerá cuando el agente lo entregue.')}</div>
          {selected.conversation_id && <Button size="sm" onClick={() => void openConversation()} disabled={error || loading || opening}><Clock3 size={14} aria-hidden />{opening ? 'Abriendo…' : 'Abrir conversación'}</Button>}
          {chatError && <p role="alert">No se pudo abrir la conversación. El encargo se conserva.</p>}
          {!selected.conversation_id && <p className={styles.hint}>Este encargo aún no tiene una conversación disponible.</p>}
        </aside>}
      </div>
    </>}
    <section className={styles.approvals} aria-label="Aprobaciones de tareas"><h2>{selected ? 'Aprobaciones de esta tarea' : 'Necesitan tu aprobación'}</h2>
      {approvals.error && <p role="alert">No se pudieron actualizar las aprobaciones. Revisa la conexión antes de decidir.</p>}
      {approvals.isLoading && <p role="status">Consultando aprobaciones…</p>}
      {!approvals.isLoading && !approvals.error && !pending.length && <p className={styles.hint}>{selected ? (selected.approval_ids === undefined ? 'El servidor aún no informa qué aprobaciones corresponden a esta tarea. Puedes consultar todas abajo.' : 'No hay aprobaciones pendientes vinculadas a esta tarea.') : 'No hay aprobaciones de acciones pendientes.'}</p>}
      <fieldset disabled={approvals.error || approvals.isLoading || error} className={styles.inboxItems}>{pending.map(item => <ApprovalCard key={item.proposal_id} approval={item} onResolved={refresh} />)}</fieldset>
      {selected && <Button variant="ghost" size="sm" onClick={() => setSelectedId(null)}>Ver todas las aprobaciones</Button>}
    </section>
  </>
}
