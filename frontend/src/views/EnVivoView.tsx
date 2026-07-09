/**
 * EnVivoView — "En vivo": watch the agents' browser work in real time (sharp, via
 * noVNC) and stop a task if something goes wrong. Teaching now lives in Habilidades.
 * The live browser frame only shows when the jailed browser actually has a REAL
 * (non-blank) page open (runtime_status.browser_live), not merely because a tool
 * whose name starts with "browser" was called (which lies on a failed launch).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { Loader2, MonitorPlay, Square } from 'lucide-react'
import { useT } from '../lib/i18n'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { ListRow } from '../components/ui/ListRow'
import { AnimatePresence, AnimatedListItem } from '../components/ui/motion'
import { VncFrame } from '../components/VncView'
import { listRecentTasks, cancelTask, getRuntimeStatus, ApiError } from '../api/client'
import type { RecentTask } from '../api/types'
import css from './EnVivoView.module.css'

function ActividadPanel() {
  const t = useT()
  const [tasks, setTasks] = useState<RecentTask[]>([])
  const [cancelling, setCancelling] = useState<string | null>(null)
  // Sticky: once a running task touches the browser, keep the live frame visible
  // (live_activity only reports the LATEST tool, so it flickers off between calls).
  const browserSeen = useRef(false)
  const [showLive, setShowLive] = useState(false)

  const refresh = useCallback(async () => {
    // Each source fails independently — a tasks-list error must never blind the
    // live panel (chat tasks aren't always listed there anyway), and vice versa.
    const [r, status] = await Promise.all([
      listRecentTasks(40).catch(() => null),
      getRuntimeStatus().catch(() => null),
    ])
    const running = (r?.tasks ?? []).filter((t) => t.status === 'in_progress')
    setTasks(running)
    // The live frame keys on browser_live (the REAL jailed-browser page state), NOT on
    // a tool name in activity[] — a failed browser_navigate / a web_search never opens
    // a real page, so they must not show the frame. Same source as the chat chip.
    const activity = status?.activity ?? []
    const browserNow = !!status?.browser_live
    const anythingRunning = activity.length > 0
      || (status?.active_task_count ?? 0) > 0
      || running.length > 0
    if (browserNow) browserSeen.current = true
    if (!anythingRunning) browserSeen.current = false // reset only when ALL idle
    setShowLive(browserSeen.current)
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
      sileo.success({ title: t('envivo.stopping') })
      setTimeout(() => void refresh(), 1500)
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : t('envivo.err.stop') })
    } finally {
      setCancelling(null)
    }
  }

  return (
    <div className={css.panel}>
      {showLive ? (
        <VncFrame viewOnly />
      ) : (
        <EmptyState
          icon={<MonitorPlay size={32} />}
          title={tasks.length > 0 ? t('envivo.no_browser_yet') : t('envivo.no_tasks')}
        />
      )}

      {/* Only render the running-tasks section when there IS something running —
          when idle it would just repeat the empty state above. */}
      {tasks.length > 0 && (
        <section className={css.section} aria-label={t('envivo.running_tasks')}>
          <div className={css.sectionHead}>
            <h2 className={css.sectionLabel}>{t('envivo.running_tasks')}</h2>
            <span className={css.countChip}>{tasks.length}</span>
          </div>

          <ul className="cv-list" role="list">
            <AnimatePresence initial={false}>
              {tasks.map((task) => (
                <AnimatedListItem key={task.task_id}>
                  <ListRow
                    className={css.taskRow}
                    icon={<Loader2 size={14} className="spin" aria-hidden="true" />}
                    label={task.label || task.name || task.task_id}
                    actions={
                      <Button
                        variant="danger"
                        size="sm"
                        loading={cancelling === task.task_id}
                        onClick={() => void handleCancel(task.task_id!)}
                        disabled={!task.task_id}
                        aria-label={t('envivo.stop.aria')}
                      >
                        <Square size={12} aria-hidden="true" />
                        {t('envivo.stop')}
                      </Button>
                    }
                  />
                </AnimatedListItem>
              ))}
            </AnimatePresence>
          </ul>
        </section>
      )}
    </div>
  )
}

export default function EnVivoView() {
  const t = useT()
  return (
    <>
      <PageHeader
        title={t('nav.envivo')}
        subtitle={t('envivo.subtitle')}
      />
      <div className="view-body">
        <ActividadPanel />
      </div>
    </>
  )
}
