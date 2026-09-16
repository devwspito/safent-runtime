import { useEffect, useRef, useState } from 'react'
import { Popover } from '@base-ui/react/popover'
import { Bell, ChevronRight, RefreshCw, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { listNotifications, getUnreadCount, markNotificationRead, markAllNotificationsRead } from '../api/client'
import type { Notification } from '../api/types'
import { useLocale, useT } from '../lib/i18n'
import { Button } from './ui/Button'
import css from './NotificationsPanel.module.css'

interface Props { loadConversation(id: string): Promise<void> }

/** The server confirms read state; opening a notification is not an approval. */
export default function NotificationsPanel({ loadConversation }: Props) {
  const navigate = useNavigate()
  const t = useT()
  const { locale } = useLocale()
  const [open, setOpen] = useState(false)
  const [count, setCount] = useState<number | null>(null)
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const closeButton = useRef<HTMLButtonElement>(null)
  const state = useRef({ active: false, open: false, list: 0, count: 0, counting: false, action: false })

  async function refreshCount(force = false) {
    const current = state.current
    if (!current.active || (current.counting && !force)) return
    current.counting = true
    const revision = ++current.count
    try {
      const value = await getUnreadCount()
      if (!Number.isSafeInteger(value.count) || value.count < 0) throw new Error('invalid_count')
      if (current.active && current.count === revision) setCount(value.count)
    } catch {
      if (current.active && current.count === revision) setCount(null)
    } finally {
      if (current.count === revision) current.counting = false
    }
  }
  useEffect(() => {
    const current = { active: true, open: false, list: 0, count: 0, counting: false, action: false }
    state.current = current
    void refreshCount()
    const timer = setInterval(() => void refreshCount(), 10_000)
    return () => { current.active = false; clearInterval(timer) }
  }, [])

  async function loadPanel() {
    const current = state.current
    if (!current.active || !current.open || current.action) return
    const revision = ++current.list
    setLoading(true)
    setError('')
    try {
      const values = await listNotifications()
      if (!Array.isArray(values)) throw new Error('invalid_list')
      if (current.active && current.open && revision === current.list) setNotifications(values)
    } catch {
      if (current.active && current.open && revision === current.list) setError(t('notifications.load_error'))
    } finally {
      if (current.active && current.open && revision === current.list) setLoading(false)
    }
  }
  function changeOpen(value: boolean) {
    const current = state.current
    current.open = value
    current.list++
    setOpen(value)
    if (value) void loadPanel()
  }
  async function actOnNotification(notification: Notification | null) {
    const current = state.current
    if (current.action || loading || !current.active || !current.open) return
    current.action = true
    const revision = ++current.list
    setPending(notification?.id ?? 'all')
    setError('')
    try {
      if (!notification) await markAllNotificationsRead()
      else if (!notification.read) await markNotificationRead(notification.id)
      if (!current.active || !current.open || current.list !== revision) return
      setNotifications(values => values.map(value => !notification || value.id === notification.id ? { ...value, read: true } : value))
      void refreshCount(true)
      if (notification?.conversation_id) {
        try {
          await loadConversation(notification.conversation_id)
          if (!current.active || !current.open || current.list !== revision) return
          changeOpen(false)
          navigate('/chat')
        } catch {
          if (current.active && current.open && current.list === revision) setError(t('notifications.open_error'))
        }
      }
    } catch {
      if (current.active && current.open && current.list === revision) setError(t('notifications.read_error'))
    } finally {
      current.action = false
      if (current.active) {
        setPending(null)
        if (current.open && current.list !== revision) void loadPanel()
      }
    }
  }
  const label = count === null ? t('notifications.count_unknown') : count > 0
    ? t('notifications.unread').replace('{count}', String(count)) : t('notifications.title')

  return <Popover.Root open={open} onOpenChange={changeOpen}>
    <Popover.Trigger className="notif-bell-btn" aria-label={label}>
      <Bell size={16} aria-hidden />
      {count !== null && count > 0 && <span className="notif-bell-badge" aria-hidden>{count > 99 ? '99+' : count}</span>}
    </Popover.Trigger>
    <Popover.Portal>
      <Popover.Positioner side="bottom" align="start" sideOffset={8} collisionPadding={8} className={css.positioner}>
        <Popover.Popup className={css.panel} initialFocus={closeButton}>
          <header className={css.header}>
            <Popover.Title>{t('notifications.title')}</Popover.Title>
            <div className={css.actions}>
              <Button size="sm" variant="ghost" disabled={loading || pending !== null} aria-label={t('notifications.refresh')} onClick={() => { void loadPanel(); void refreshCount(true) }}><RefreshCw size={14} aria-hidden /></Button>
              <Popover.Close ref={closeButton} className="cv-btn cv-btn--ghost cv-btn--sm" aria-label={t('dialog.close')}><X size={15} aria-hidden /></Popover.Close>
            </div>
          </header>
          <Popover.Description className={css.description}>{t('notifications.description')}</Popover.Description>
          {error && <div role="alert" className={css.error}><p>{error}</p><Button size="sm" variant="ghost" disabled={pending !== null} onClick={() => void loadPanel()}>{t('notifications.retry')}</Button></div>}
          <div className={css.body} aria-busy={loading}>
            {loading ? <p role="status" className={css.empty}>{t('notifications.loading')}</p>
              : !error && notifications.length === 0 ? <p className={css.empty}>{t('notifications.empty')}</p>
              : <ul className={css.list}>{notifications.map(notification => <li key={notification.id}>
                <button className={css.item} data-unread={!notification.read} disabled={pending !== null} onClick={() => void actOnNotification(notification)}>
                  <span className={css.dot} data-status={notification.status} aria-hidden />
                  <span className={css.content}>
                    <span className={css.title}>{notification.title}</span>
                    {notification.body && <span className={css.summary}>{notification.body}</span>}
                    <time className={css.time} dateTime={notification.created_at}>{Number.isFinite(Date.parse(notification.created_at)) ? new Date(notification.created_at).toLocaleString(locale, { dateStyle: 'short', timeStyle: 'short' }) : t('notifications.time_unknown')}</time>
                    {pending === notification.id && <span role="status">{t('notifications.opening')}</span>}
                  </span>
                  {notification.conversation_id && <ChevronRight size={14} aria-hidden />}
                </button>
              </li>)}</ul>}
          </div>
          {!error && notifications.some(value => !value.read) && <footer className={css.footer}><Button size="sm" variant="ghost" disabled={loading || pending !== null} loading={pending === 'all'} onClick={() => void actOnNotification(null)}>{t('notifications.mark_all')}</Button></footer>}
        </Popover.Popup>
      </Popover.Positioner>
    </Popover.Portal>
  </Popover.Root>
}
