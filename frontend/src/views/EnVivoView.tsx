/**
 * EnVivoView — unified "En vivo" section: two tabs, both showing the jailed browser
 * SHARP + FLUID via noVNC (headful Chromium on Xvfb + x11vnc, industry-standard —
 * no more blurry CDP screencast).
 *   · Actividad: view-only live watch of the agent's browser + running tasks (Detener).
 *   · Enseñar:   drive the real browser to demonstrate a skill; steps are recorded.
 */
import { useCallback, useEffect, useState } from 'react'
import { sileo } from 'sileo'
import { Square } from 'lucide-react'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { VncView } from '../components/VncView'
import {
  listRecentTasks,
  cancelTask,
  ApiError,
  startTeaching,
  signTeaching,
} from '../api/client'
import type { RecentTask } from '../api/types'

type Tab = 'actividad' | 'ensenar'

function VncFrame({ viewOnly }: { viewOnly?: boolean }) {
  return (
    <div
      style={{
        position: 'relative',
        width: '100%',
        aspectRatio: '16 / 9',
        maxHeight: 'min(74vh, 900px)',
        background: '#000',
        border: '1px solid var(--color-border-subtle)',
        borderRadius: 'var(--radius-md)',
        overflow: 'hidden',
      }}
    >
      <VncView viewOnly={viewOnly} />
    </div>
  )
}

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

  const hasRunning = tasks.length > 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>
      {hasRunning ? (
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
          No hay ninguna tarea en ejecución ahora mismo. Cuando un agente empiece a
          trabajar, verás aquí su navegador en directo (nítido) y podrás detenerlo.
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

function EnsenarPanel() {
  const [skill, setSkill] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleStart() {
    if (!skill.trim()) { sileo.error({ title: 'Ponle un nombre a la habilidad' }); return }
    setBusy(true)
    try {
      const r = await startTeaching(skill.trim())
      setSessionId(r.session_id)
      sileo.success({ title: 'Grabando. Demuestra la tarea en el navegador de abajo.' })
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : 'No se pudo iniciar' })
    } finally {
      setBusy(false)
    }
  }

  async function handleSave() {
    if (!sessionId) return
    setBusy(true)
    try {
      await signTeaching(sessionId)
      sileo.success({ title: '¡Habilidad guardada! Ábrela en Habilidades.' })
      setSessionId(null)
      setSkill('')
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : 'No se pudo guardar' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          type="text"
          value={skill}
          onChange={(e) => setSkill(e.target.value)}
          disabled={!!sessionId}
          placeholder='Nombre de la habilidad (p. ej. "Reservar plaza")'
          style={{
            flex: 1, minWidth: 240,
            padding: 'var(--space-2) var(--space-3)',
            border: '1px solid var(--color-border-subtle)',
            borderRadius: 'var(--radius-md)',
            background: 'var(--color-bg-subtle)', color: 'var(--color-text)',
            fontSize: 'var(--text-sm)',
          }}
        />
        {sessionId ? (
          <Button variant="primary" size="sm" loading={busy} onClick={() => void handleSave()}>
            Guardar habilidad
          </Button>
        ) : (
          <Button variant="primary" size="sm" loading={busy} onClick={() => void handleStart()}>
            Empezar a enseñar
          </Button>
        )}
      </div>
      <p style={{ color: 'var(--color-text-dim)', fontSize: 'var(--text-sm)', margin: 0 }}>
        {sessionId
          ? 'Navega y haz la tarea en el navegador de abajo, como lo harías normalmente. Cuando termines, pulsa «Guardar habilidad».'
          : 'Ponle nombre y pulsa «Empezar a enseñar». Se abre un navegador real (nítido) que puedes conducir; tus pasos se convierten en una habilidad reutilizable.'}
      </p>
      <VncFrame />
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
        {tab === 'ensenar' && <EnsenarPanel />}
      </div>
    </>
  )
}
