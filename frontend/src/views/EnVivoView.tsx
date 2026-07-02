/**
 * EnVivoView — "En vivo": watch the agents' browser work in real time (sharp, via
 * noVNC) and stop a task if something goes wrong. Teaching now lives in Habilidades.
 * The live browser frame only shows when a running task is actually USING the browser
 * (live_activity tool starts with "browser"), not for every running task.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { Square } from 'lucide-react'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { VncFrame } from '../components/VncView'
import { listRecentTasks, cancelTask, getRuntimeStatus, ApiError } from '../api/client'
import type { RecentTask } from '../api/types'

function ActividadPanel() {
  const [tasks, setTasks] = useState<RecentTask[]>([])
  const [cancelling, setCancelling] = useState<string | null>(null)
  // Sticky: once a running task touches the browser, keep the live frame visible
  // (live_activity only reports the LATEST tool, so it flickers off between calls).
  const browserSeen = useRef(false)
  const [showLive, setShowLive] = useState(false)

  const refresh = useCallback(async () => {
    const [r, status] = await Promise.all([
      listRecentTasks(40),
      getRuntimeStatus().catch(() => null),
    ])
    const running = (r.tasks ?? []).filter((t) => t.status === 'in_progress')
    setTasks(running)
    const browserNow = !!status?.activity?.some((a) => (a.tool ?? '').startsWith('browser'))
    if (browserNow) browserSeen.current = true
    if (running.length === 0) browserSeen.current = false // reset when idle
    setShowLive(running.length > 0 && browserSeen.current)
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
      {showLive ? (
        <VncFrame viewOnly />
      ) : (
        <div style={{
          border: '1px dashed var(--color-border-subtle)',
          borderRadius: 'var(--radius-md)',
          padding: 'var(--space-8) var(--space-4)',
          textAlign: 'center',
          color: 'var(--color-text-muted)',
          fontSize: 'var(--text-sm)',
        }}>
          {tasks.length > 0
            ? 'Hay tareas en ejecución, pero ninguna está usando el navegador ahora mismo. Cuando un agente navegue, verás aquí su navegador en directo (nítido).'
            : 'No hay ninguna tarea en ejecución. Cuando un agente empiece a navegar, verás aquí su navegador en directo y podrás detenerlo.'}
        </div>
      )}

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
  return (
    <>
      <PageHeader
        title="En vivo"
        subtitle="Observa a tus agentes trabajar en tiempo real y detén una tarea si algo va mal. Para enseñar una habilidad nueva, ve a Habilidades."
      />
      <div className="view-body">
        <ActividadPanel />
      </div>
    </>
  )
}
