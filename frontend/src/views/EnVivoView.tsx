/**
 * EnVivoView — unified "En vivo" section (option A): two tabs.
 *   · Actividad: live view of the agent's internal browser + running tasks with Detener.
 *   · Enseñar:   the teaching flow (demonstrate a skill). Verificar switches here.
 */
import { useCallback, useEffect, useState } from 'react'
import { sileo } from 'sileo'
import { Square } from 'lucide-react'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { AgentLiveWatch } from '../components/AgentLiveWatch'
import { listRecentTasks, cancelTask, ApiError } from '../api/client'
import type { RecentTask } from '../api/types'
import TeachingView from './TeachingView'

type Tab = 'actividad' | 'ensenar'

function ActividadPanel() {
  const [tasks, setTasks] = useState<RecentTask[]>([])
  const [cancelling, setCancelling] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    const r = await listRecentTasks(40)
    setTasks((r.tasks ?? []).filter((t) => t.status === 'in_progress'))
  }, [])

  useEffect(() => {
    void refresh()
    const id = setInterval(() => void refresh(), 4000)
    return () => clearInterval(id)
  }, [refresh])

  async function handleCancel(id: string) {
    setCancelling(id)
    try {
      await cancelTask(id)
      sileo.success({ title: 'Deteniendo la tarea…' })
      setTimeout(() => void refresh(), 1500)
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : 'No se pudo detener la tarea' })
    } finally {
      setCancelling(null)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>
      <AgentLiveWatch label="Actividad del agente en vivo" />

      <section>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginBottom: 'var(--space-3)' }}>
          <h2 style={{ fontSize: 'var(--text-base)', fontWeight: 'var(--weight-semibold)', margin: 0 }}>
            Tareas en ejecución
          </h2>
          {tasks.length > 0 && (
            <span style={{
              fontSize: 'var(--text-xs)', color: 'var(--color-text-muted)',
              background: 'var(--color-bg-subtle)', borderRadius: 'var(--radius-sm)',
              padding: '1px 7px',
            }}>{tasks.length}</span>
          )}
        </div>

        {tasks.length === 0 && (
          <p style={{ color: 'var(--color-text-muted)', fontSize: 'var(--text-sm)' }}>
            No hay tareas ejecutándose ahora mismo. Cuando un agente esté trabajando aparecerá aquí
            y podrás detenerlo.
          </p>
        )}

        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
          {tasks.map((t) => (
            <li
              key={t.task_id}
              style={{
                display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
                border: '1px solid var(--color-border-subtle)', borderRadius: 'var(--radius-md)',
                padding: 'var(--space-3) var(--space-4)',
              }}
            >
              <span style={{
                flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                fontSize: 'var(--text-sm)',
              }}>
                {t.label || t.name || t.task_id}
              </span>
              <Button
                variant="danger"
                size="sm"
                loading={cancelling === t.task_id}
                onClick={() => void handleCancel(t.task_id!)}
                disabled={!t.task_id}
                aria-label="Detener esta tarea"
              >
                <Square size={12} aria-hidden="true" />
                Detener
              </Button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

export default function EnVivoView() {
  const [tab, setTab] = useState<Tab>('actividad')

  return (
    <>
      <PageHeader
        title="En vivo"
        subtitle="Observa a tus agentes trabajar en tiempo real, detén una tarea si algo va mal, o enséñales una nueva habilidad."
      />
      <div className="view-body">
        <div
          role="tablist"
          aria-label="Secciones en vivo"
          style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-5)' }}
        >
          <Button variant={tab === 'actividad' ? 'primary' : 'ghost'} size="sm" onClick={() => setTab('actividad')}>
            Actividad
          </Button>
          <Button variant={tab === 'ensenar' ? 'primary' : 'ghost'} size="sm" onClick={() => setTab('ensenar')}>
            Enseñar
          </Button>
        </div>

        {tab === 'actividad' && <ActividadPanel />}
        {tab === 'ensenar' && <TeachingView embedded onVerifyStarted={() => setTab('actividad')} />}
      </div>
    </>
  )
}
