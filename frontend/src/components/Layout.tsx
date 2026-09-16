import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import {
  PanelLeft, Search, MessageSquare, RefreshCw, ListTodo,
  MoreHorizontal, Archive, ArchiveRestore, Trash2,
} from 'lucide-react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'
import {
  listConversations, archiveConversation, unarchiveConversation, deleteConversation,
} from '../api/client'
import { useChat } from '../hooks/useChat'
import { useFeatures } from '../hooks/useFeatures'
import { usePendingApprovals } from '../hooks/usePendingApprovals'
import { usePendingInboundDelegations } from '../hooks/usePendingInboundDelegations'
import { useAdsAvailability } from '../hooks/useAdsAvailability'
import { AdsNavItem } from './AdsNavItem'
import type { ConversationSummary } from '../api/types'
import NotificationsPanel from './NotificationsPanel'
import KillSwitchBanner from './KillSwitchBanner'
import { SystemUpdateFooter } from './SystemUpdateFooter'
import { useT, useLocale } from '../lib/i18n'
import { CAPACIDADES_VIEW_IDS, SISTEMA_VIEW_IDS } from '../views/sectionHubIds'
import styles from './Layout.module.css'
import { ChatDrafts, type ChatDraft } from '../lib/chatDrafts'
import { AdsWorkspaceContext } from './AdsWorkspaceContext'

// activeProviderReload lets child views (ProvidersView) trigger a re-check after
// connecting a model. The "Falta conectar un modelo" nudge was removed — the chat
// shows its own in-chat no-model alert, so the sidebar nudge is redundant.
export interface LayoutProps {
  activeProviderReload(): void
}


interface NavItem {
  to: string
  label: string
  icon: React.ReactNode
}


function ChatIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2 3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 3V3Z"
        stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

function TasksIcon() { return <ListTodo className="nav-icon" size={16} aria-hidden /> }

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function CapacidadesIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2 14 5 8 8 2 5 8 2Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M2 8.5 8 11.5 14 8.5M2 12 8 15 14 12"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function SistemaIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2 5h8M13 5h1M2 11h1M6 11h8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="11.5" cy="5" r="1.6" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="4.5" cy="11" r="1.6" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

interface HubNavItem extends NavItem {
  /** Visible when ANY of these backend view ids is allowed (hub aggregates them). */
  anyOf?: readonly string[]
  /** Show the pending-approvals badge on this item. */
  showsPendingBadge?: boolean
}

/**
 * Four clean entries (owner decision): Chat · Tareas · Capacidades · Sistema.
 * The two hubs contain every other section as tabs (see SectionHubs.tsx).
 * A fifth, "Anuncios", is appended UNCONDITIONALLY by Layout below (026,
 * FR-001/Assumption 7) — not a feature-gated hub tab, and never hidden;
 * see AdsNavItem/useAdsAvailability for its disabled presentation.
 */
function useNavItems(): HubNavItem[] {
  const t = useT()
  return [
    { to: '/chat',        label: t('nav.chat'),                 icon: <ChatIcon /> },
    { to: '/tareas',      label: t('nav.tareas'),               icon: <TasksIcon /> },
    { to: '/capacidades', label: t('nav.section.capabilities'), icon: <CapacidadesIcon />, anyOf: CAPACIDADES_VIEW_IDS },
    { to: '/sistema',     label: t('nav.section.system'),       icon: <SistemaIcon />, anyOf: SISTEMA_VIEW_IDS, showsPendingBadge: true },
  ]
}

// ── Recientes ─────────────────────────────────────────────────────────────────

const PREVIEW_COUNT = 8
const UNDO_TIMEOUT_MS = 6_000

/** Conversation summaries from older backends key the id as `conversation_id`. */
function conversationId(c: ConversationSummary): string | undefined {
  return (c as ConversationSummary & { conversation_id?: string }).conversation_id ?? c.id
}

function relativeTime(iso: string | undefined, t: ReturnType<typeof useT>): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return t('layout.time.now')
  if (mins < 60) return t('layout.time.mins_ago').replace('{n}', String(mins))
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return t('layout.time.hours_ago').replace('{n}', String(hrs))
  return t('layout.time.days_ago').replace('{n}', String(Math.floor(hrs / 24)))
}

// ── ChatOutletContext — shared between RecentsSection (in nav) and ChatView ──

export interface ChatOutletContext {
  draft: ChatDraft
  convId: string | null
  /** Agent bound to the current conversation (null = CEO / default). */
  agentId: string | null
  /** Display name of the bound agent (set when opening a chat from an agent card). */
  agentName: string | null
  loadConversation(id: string): Promise<void>
  startNew(): void
  /** Start a new conversation pre-bound to a specific agent, then navigate to chat. */
  startNewWithAgent(agentId: string, agentName: string): void
  sendMessage(text: string): Promise<void>
  messages: ReturnType<typeof useChat>['messages']
  status: ReturnType<typeof useChat>['status']
  stopStream(): void
  /** Incremented each time the user sends a message — signals PendingApprovalsInChat to poll immediately. */
  approvalRefreshTick: number
  /** Incremented after a turn finishes (conversation persisted) — signals RecentsSection to refetch. */
  conversationsTick: number
  /** Call after a provider is connected/activated to trigger an immediate re-check of the nudge state. */
  reloadProvider(): void
  /** True while re-attaching to a stream that was in-flight before a page refresh. */
  reconnecting: boolean
  streamError: boolean
  cancellation: ReturnType<typeof useChat>['cancellation']
  /** Sticky: the in-flight turn's task is using the browser → chat can show live view. */
  liveBrowserActive: boolean
}

interface RecentsSectionProps {
  activeConvId: string | null
  conversationsTick: number
  loadConversation(id: string): Promise<void>
  /** Called when the open conversation is archived or deleted, so the chat never shows a dead thread. */
  startNew(): void
}

export function RecentsSection({ activeConvId, conversationsTick, loadConversation, startNew }: RecentsSectionProps) {
  const t = useT()
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const [error, setError] = useState(false)
  const [query, setQuery] = useState('')
  const [opening, setOpening] = useState<string | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [openMenuId, setOpenMenuId] = useState<string | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deleteErrorId, setDeleteErrorId] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [undo, setUndo] = useState<{ id: string; title: string } | null>(null)
  const request = useRef(0)
  const selecting = useRef(false)
  const undoTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuBtnRefs = useRef(new Map<string, HTMLButtonElement>())
  const cancelBtnRef = useRef<HTMLButtonElement>(null)

  const load = useCallback(() => {
    const version = ++request.current
    setLoading(true)
    setError(false)
    listConversations(undefined, { includeArchived: true })
      .then(data => {
        if (version !== request.current) return
        setConversations(Array.isArray(data) ? data : [])
        setLoading(false)
      })
      .catch(() => {
        if (version !== request.current) return
        setError(true)
        setLoading(false)
      })
  }, [])

  useEffect(() => {
    load()
    return () => { request.current += 1 }
  }, [activeConvId, conversationsTick, load])

  // The undo notice is 6 s of retained intent — never leak the timer past unmount.
  useEffect(() => () => { if (undoTimer.current) clearTimeout(undoTimer.current) }, [])

  async function handleSelect(id: string) {
    if (selecting.current) return
    selecting.current = true
    setOpening(id)
    try {
      await loadConversation(id)
      navigate('/chat')
    } catch {
      setError(true)
    } finally {
      selecting.current = false
      setOpening(null)
    }
  }

  // ── Row menu ("⋯") — one at a time; outside click / Escape close it and
  // return focus to its trigger, mirroring Composer's context menu (ChatView.tsx).
  function closeMenu() {
    setOpenMenuId(null)
  }

  function toggleMenu(id: string) {
    setOpenMenuId(current => (current === id ? null : id))
  }

  useEffect(() => {
    if (!openMenuId) return
    const onDoc = (e: MouseEvent) => {
      const tgt = e.target as Node
      if (menuRef.current?.contains(tgt) || menuBtnRefs.current.get(openMenuId)?.contains(tgt)) return
      setOpenMenuId(null)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [openMenuId])

  useEffect(() => {
    if (openMenuId) menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
  }, [openMenuId])

  // Cancelling/exiting confirm mode unmounts its Cancelar button and remounts the
  // row's "⋯" trigger in the SAME commit — the trigger's ref is only populated
  // once that render lands, so the id to refocus is queued in a ref, not read
  // from the (already-detached) menuBtnRefs entry at cancel-time.
  const refocusTriggerId = useRef<string | null>(null)
  useEffect(() => {
    if (confirmDeleteId) {
      cancelBtnRef.current?.focus()
    } else if (refocusTriggerId.current) {
      menuBtnRefs.current.get(refocusTriggerId.current)?.focus()
      refocusTriggerId.current = null
    }
  }, [confirmDeleteId])

  function handleMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>, id: string) {
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpenMenuId(null)
      menuBtnRefs.current.get(id)?.focus()
      return
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    const items = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
    const current = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1
      : (current + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
    items[next]?.focus()
  }

  function handleConfirmKeyDown(e: ReactKeyboardEvent<HTMLDivElement>, id: string) {
    if (e.key !== 'Escape') return
    e.preventDefault()
    cancelDeleteConfirm(id)
  }

  // ── Archive (immediate) + undo ───────────────────────────────────────────
  function scheduleUndoDismiss() {
    if (undoTimer.current) clearTimeout(undoTimer.current)
    undoTimer.current = setTimeout(() => setUndo(null), UNDO_TIMEOUT_MS)
  }

  async function handleArchive(id: string, title: string) {
    closeMenu()
    setPendingId(id)
    try {
      await archiveConversation(id)
      setConversations(prev => prev.map(c => conversationId(c) === id ? { ...c, archived: true } : c))
      if (id === activeConvId) startNew()
      setUndo({ id, title })
      scheduleUndoDismiss()
    } catch {
      sileo.error({ title: t('layout.recents.archive_err') })
    } finally {
      setPendingId(null)
    }
  }

  async function handleUndoArchive() {
    const target = undo
    if (!target) return
    if (undoTimer.current) { clearTimeout(undoTimer.current); undoTimer.current = null }
    setUndo(null)
    try {
      await unarchiveConversation(target.id)
      setConversations(prev => prev.map(c => conversationId(c) === target.id ? { ...c, archived: false } : c))
    } catch {
      sileo.error({ title: t('layout.recents.unarchive_err') })
    }
  }

  async function handleRestore(id: string) {
    closeMenu()
    setPendingId(id)
    try {
      await unarchiveConversation(id)
      setConversations(prev => prev.map(c => conversationId(c) === id ? { ...c, archived: false } : c))
    } catch {
      sileo.error({ title: t('layout.recents.unarchive_err') })
    } finally {
      setPendingId(null)
    }
  }

  // ── Delete (inline confirm) ──────────────────────────────────────────────
  function startDeleteConfirm(id: string) {
    closeMenu()
    setDeleteErrorId(null)
    setConfirmDeleteId(id)
  }

  function cancelDeleteConfirm(id: string) {
    refocusTriggerId.current = id
    setConfirmDeleteId(null)
    setDeleteErrorId(null)
  }

  async function confirmDelete(id: string) {
    setPendingId(id)
    setDeleteErrorId(null)
    try {
      await deleteConversation(id)
      setConversations(prev => prev.filter(c => conversationId(c) !== id))
      setConfirmDeleteId(null)
      if (id === activeConvId) startNew()
    } catch {
      setDeleteErrorId(id)
    } finally {
      setPendingId(null)
    }
  }

  function toggleArchivedView() {
    setShowArchived(v => !v)
    setExpanded(false)
    closeMenu()
    setConfirmDeleteId(null)
  }

  const archivedList = conversations.filter(c => c.archived)
  const baseList = showArchived ? archivedList : conversations.filter(c => !c.archived)
  const filtered = baseList.filter(c => (c.title ?? t('layout.recents.untitled')).toLocaleLowerCase().includes(query.toLocaleLowerCase()))
  const visible = query || expanded ? filtered : filtered.slice(0, PREVIEW_COUNT)
  const overflow = baseList.length - PREVIEW_COUNT

  if (loading && conversations.length === 0) {
    return (
      <div className="sidebar-recents" aria-label={t('layout.recents.aria')}>
        <div className="sidebar-section-label">{t('layout.recents.label')}</div>
        {Array.from({ length: PREVIEW_COUNT }, (_, i) => (
          <div
            key={i}
            className="skeleton skeleton--block"
            style={{
              margin: '2px var(--space-2)',
              animationDelay: `${i * 80}ms`,
            }}
            aria-hidden="true"
          />
        ))}
      </div>
    )
  }

  return (
    <div className="sidebar-recents" aria-label={t('layout.recents.aria')}>
      <div className="sidebar-section-label">{t('layout.recents.label')}</div>
      {conversations.length > 0 && <label className={styles.search}>
        <Search size={14} aria-hidden="true" />
        <input type="search" value={query} onChange={e => setQuery(e.target.value)}
          aria-label={t('layout.recents.search')} placeholder={t('layout.recents.search')} />
      </label>}
      {error && <div className={styles.recentsError} role="status">
        <span>{t('layout.recents.error')}</span>
        <button type="button" onClick={load} disabled={loading} aria-label={t('approval.err.retry')}>
          <RefreshCw size={14} aria-hidden="true" />
        </button>
      </div>}
      {query && visible.length === 0 && <p className="recent-empty">{t('layout.recents.no_results')}</p>}
      {!query && !error && visible.length === 0 && (
        <p className="recent-empty">{showArchived ? t('layout.recents.empty_archived') : t('layout.recents.empty')}</p>
      )}
      <ul aria-label={t('layout.recents.aria')} aria-busy={loading}>
        {visible.map(c => {
          const id = conversationId(c)
          if (!id) return null
          const title = c.title ?? t('layout.recents.untitled')
          const time = relativeTime(
            (c as ConversationSummary & { last_msg_at?: string }).last_msg_at
            ?? c.updated_at
            ?? c.created_at,
            t,
          )
          const isActive = id === activeConvId
          const menuOpen = openMenuId === id
          const confirming = confirmDeleteId === id
          const hasDeleteError = deleteErrorId === id
          const busy = pendingId === id

          return (
            <li key={id} className={styles.recentRow}>
              {confirming ? (
                <div className={styles.confirmRow} onKeyDown={e => handleConfirmKeyDown(e, id)}>
                  {hasDeleteError ? (
                    <>
                      <span className={styles.confirmText}>{t('layout.recents.delete_err')}</span>
                      <div className={styles.confirmActions}>
                        <button type="button" className={styles.confirmBtnGhost} disabled={busy}
                          onClick={() => void confirmDelete(id)}>
                          {t('approval.err.retry')}
                        </button>
                        <button type="button" ref={cancelBtnRef} className={styles.confirmBtnGhost} disabled={busy}
                          onClick={() => cancelDeleteConfirm(id)}>
                          {t('layout.recents.cancel')}
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <span className={styles.confirmText}>{t('layout.recents.confirm_delete')}</span>
                      <div className={styles.confirmActions}>
                        <button type="button" className={styles.confirmBtnDanger} disabled={busy} aria-busy={busy}
                          onClick={() => void confirmDelete(id)}>
                          {t('layout.recents.delete')}
                        </button>
                        <button type="button" ref={cancelBtnRef} className={styles.confirmBtnGhost} disabled={busy}
                          onClick={() => cancelDeleteConfirm(id)}>
                          {t('layout.recents.cancel')}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ) : (
                <>
                  <button
                    className={`recent-item${isActive ? ' recent-item--active' : ''}`}
                    onClick={() => handleSelect(id)}
                    type="button"
                    title={title}
                    aria-current={isActive ? 'page' : undefined}
                    aria-busy={opening === id}
                    disabled={opening !== null}
                  >
                    <MessageSquare size={14} aria-hidden="true" />
                    <span className="recent-title">{title}</span>
                    {c.archived && <span className={styles.archivedTag}>{t('layout.recents.archived_tag')}</span>}
                    {time && <span className="recent-time">{time}</span>}
                  </button>
                  <button
                    ref={el => { if (el) menuBtnRefs.current.set(id, el); else menuBtnRefs.current.delete(id) }}
                    type="button"
                    className={styles.rowMenuBtn}
                    onClick={() => toggleMenu(id)}
                    aria-haspopup="menu"
                    aria-expanded={menuOpen}
                    aria-label={t('layout.recents.menu_aria')}
                  >
                    <MoreHorizontal size={14} aria-hidden="true" />
                  </button>
                  {menuOpen && (
                    <div ref={menuRef} className={styles.rowMenu} role="menu" aria-label={t('layout.recents.menu_aria')}
                      onKeyDown={e => handleMenuKeyDown(e, id)}>
                      {showArchived ? (
                        <button type="button" role="menuitem" className={styles.rowMenuItem}
                          onClick={() => void handleRestore(id)}>
                          <ArchiveRestore size={14} aria-hidden="true" /> {t('layout.recents.restore')}
                        </button>
                      ) : (
                        <button type="button" role="menuitem" className={styles.rowMenuItem}
                          onClick={() => void handleArchive(id, title)}>
                          <Archive size={14} aria-hidden="true" /> {t('layout.recents.archive')}
                        </button>
                      )}
                      <button type="button" role="menuitem" className={`${styles.rowMenuItem} ${styles.rowMenuItemDanger}`}
                        onClick={() => startDeleteConfirm(id)}>
                        <Trash2 size={14} aria-hidden="true" /> {t('layout.recents.delete')}
                      </button>
                    </div>
                  )}
                </>
              )}
            </li>
          )
        })}
        {!query && overflow > 0 && (
          <li>
            <button
              className="recent-item text-accent"
              onClick={() => setExpanded(v => !v)}
              type="button"
              aria-expanded={expanded}
            >
              {expanded ? t('layout.recents.less') : t('layout.recents.more').replace('{n}', String(overflow))}
            </button>
          </li>
        )}
        {!query && (archivedList.length > 0 || showArchived) && (
          <li>
            <button
              className="recent-item text-accent"
              onClick={toggleArchivedView}
              type="button"
              aria-expanded={showArchived}
            >
              {showArchived ? t('layout.recents.hide_archived') : t('layout.recents.show_archived').replace('{n}', String(archivedList.length))}
            </button>
          </li>
        )}
      </ul>
      {undo && (
        <div className={styles.undoNotice} role="status">
          <span>{t('layout.recents.archived_notice')}</span>
          <button type="button" className={styles.undoNoticeBtn} onClick={() => void handleUndoArchive()}>
            {t('layout.recents.undo')}
          </button>
        </div>
      )}
    </div>
  )
}

export default function Layout({ activeProviderReload }: LayoutProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const navItems = useNavItems()
  const t = useT()
  const { locale, setLocale } = useLocale()
  const { isLoading: featuresLoading, allowed } = useFeatures()
  // "Anuncios" is a first-level entry, ALWAYS visible (026 FR-001/Assumption
  // 7) — never gated by allowed()/anyOf like the hub tabs, and never hidden
  // for lack of a connection: useAdsAvailability only drives its disabled
  // *presentation*, rides alongside (not inside) the allowed() gate.
  const adsAvailability = useAdsAvailability()
  // activeProviderReload is exposed on the outlet context so views like
  // ProvidersView can signal an immediate re-check after connecting a model.
  // The hook already self-heals via a 5 s poll; this enables instant feedback.

  // Chat state lives here, above both the sidebar nav (RecentsSection) and
  // the main content area (ChatView). ChatView receives it via outlet context.
  const chat = useChat()
  const [drafts] = useState(() => new ChatDrafts())
  const [draft, setDraft] = useState(() => chat.convId ? drafts.forConversation(chat.convId) : drafts.forNew(chat.agentId))
  const pendingDraft = useRef<{ draft: ChatDraft; agentId: string | null } | null>(null)
  useLayoutEffect(() => {
    if (!chat.convId) {
      setDraft(drafts.forNew(chat.agentId))
    } else if (pendingDraft.current) {
      const pending = pendingDraft.current
      pendingDraft.current = null
      setDraft(drafts.bind(chat.convId, pending.agentId, pending.draft))
    } else {
      setDraft(drafts.forConversation(chat.convId))
    }
  }, [chat.convId, chat.agentId, drafts])
  // Display name for the agent bound to the current chat (cleared on new chat).
  const [boundAgentName, setBoundAgentName] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(() => !window.matchMedia?.('(max-width: 700px)').matches)
  const sidebarToggle = useRef<HTMLButtonElement>(null)
  const sidebarReopen = useRef<HTMLButtonElement>(null)
  const [adsPanelActive, setAdsPanelActive] = useState(false)
  const isAdsRoute = location.pathname.replace(/\/+$/, '') === '/anuncios'
  const adsWorkspace = isAdsRoute && adsPanelActive
  const previousSafentRoute = useRef('/chat')
  const wasAdsWorkspace = useRef(false)

  useLayoutEffect(() => {
    if (!isAdsRoute) previousSafentRoute.current = location.pathname + location.search + location.hash
    if (wasAdsWorkspace.current && !adsWorkspace) {
      // The old iframe/return control is gone. Restore a visible shell target,
      // including browser Back and availability changes, without opening a drawer.
      const target = sidebarOpen
        ? document.querySelector<HTMLAnchorElement>('#community-sidebar a[href$="/anuncios"]')
        : sidebarReopen.current
      const visibleTarget = target ?? document.getElementById('main-content')
      visibleTarget?.focus()
    }
    wasAdsWorkspace.current = adsWorkspace
  }, [adsWorkspace, isAdsRoute, location.pathname, location.search, location.hash, sidebarOpen])

  const returnToSafent = useCallback(() => {
    // Do not history.back(): the iframe has its own history and direct entry
    // may otherwise leave Safent entirely. Never reset the retained chat draft.
    navigate(previousSafentRoute.current)
  }, [navigate])

  useEffect(() => {
    const media = window.matchMedia?.('(max-width: 700px)')
    if (!media) return
    const onResize = () => { if (media.matches) setSidebarOpen(false) }
    media.addEventListener?.('change', onResize)
    return () => media.removeEventListener?.('change', onResize)
  }, [])
  useEffect(() => {
    if (window.matchMedia?.('(max-width: 700px)').matches) setSidebarOpen(false)
  }, [location.key])
  useEffect(() => {
    if (!sidebarOpen || adsWorkspace) return
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !window.matchMedia?.('(max-width: 700px)').matches) return
      setSidebarOpen(false)
      requestAnimationFrame(() => sidebarReopen.current?.focus())
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [sidebarOpen, adsWorkspace])

  function toggleSidebar() {
    setSidebarOpen(open => !open)
    requestAnimationFrame(() => {
      if (sidebarOpen) sidebarReopen.current?.focus()
      else sidebarToggle.current?.focus()
    })
  }

  // Bumped each time the user sends a message so PendingApprovalsInChat can
  // fire an immediate poll without waiting for the 3 s interval.
  const [approvalRefreshTick, setApprovalRefreshTick] = useState(0)

  // Global FRESH pending-approvals count → badge on the Sistema nav item. HITL
  // cards from NON-chat cycles (scheduled / autonomous; conversation_id=null)
  // don't anchor to a chat thread, so without this they'd be invisible outside
  // the Security view. Shares the exact freshness rule with SeguridadView — a
  // stale approval must never produce a phantom badge.
  // Inbound cross-human delegations (FASE 3 A2A) share the same badge — a
  // colleague's assistant asking for help is just as "needs your attention"
  // as the agent's own HITL approvals.
  const pendingCount =
    usePendingApprovals(6000, approvalRefreshTick).approvals.length +
    usePendingInboundDelegations(6000, approvalRefreshTick).length

  async function handleSendMessage(text: string) {
    if (!chat.convId) pendingDraft.current = { draft, agentId: chat.agentId }
    await chat.sendMessage(text)
    setApprovalRefreshTick(t => t + 1)
  }

  function handleNewChat() {
    pendingDraft.current = null
    chat.startNew()
    setBoundAgentName(null)
    navigate('/chat')
  }

  function handleStartNewWithAgent(agentId: string, agentName: string) {
    pendingDraft.current = null
    chat.startNewWithAgent(agentId)
    setBoundAgentName(agentName)
    navigate('/chat')
  }

  async function handleLoadConversation(id: string) {
    pendingDraft.current = null
    setBoundAgentName(null)
    await chat.loadConversation(id)
  }

  return (
    <div className={`app-shell ${styles.shell}`} data-sidebar-open={sidebarOpen && !adsWorkspace}
      data-ads-workspace={adsWorkspace}>
      <a className={styles.skipLink} href="#main-content">{t('layout.skip')}</a>
      {!sidebarOpen && !adsWorkspace && <button ref={sidebarReopen} className={styles.reopen} type="button"
        aria-label={t('layout.sidebar.open')} aria-expanded={false} aria-controls="community-sidebar"
        onClick={toggleSidebar}><PanelLeft size={18} aria-hidden="true" /></button>}
      <nav id="community-sidebar" className={`sidebar ${styles.sidebar}`} hidden={!sidebarOpen || adsWorkspace} aria-label={t('layout.nav.aria')}>
        {/* Wordmark */}
        <div className="sidebar-wordmark">
          <div className="sidebar-wordmark-inner">
            <span className="sidebar-name">Safent</span>
          </div>
          <div className={styles.headerActions}>
            <NotificationsPanel loadConversation={handleLoadConversation} />
            <button ref={sidebarToggle} type="button" className={styles.iconButton}
              aria-label={t('layout.sidebar.close')} aria-expanded={true} aria-controls="community-sidebar"
              onClick={toggleSidebar}><PanelLeft size={18} aria-hidden="true" /></button>
          </div>
        </div>

        {/* New chat button — always resets the conversation */}
        <button
          className="sidebar-new-chat"
          aria-label={t('layout.new_chat')}
          type="button"
          onClick={handleNewChat}
        >
          <PlusIcon />
          {t('layout.new_chat')}
        </button>

        {/* Scrollable area */}
        <div className="sidebar-scroll">
          {/* Four clean entries; the hubs are visible when ANY of their child
              views is allowed. Pending-approvals badge rides on Sistema. */}
          <div className="sidebar-nav">
            {featuresLoading ? (
              <ul role="list" aria-busy="true" aria-label={t('layout.loading_nav_aria')}>
                {Array.from({ length: 4 }, (_, i) => (
                  <li key={i}>
                    <div
                      className="skeleton skeleton--block"
                      style={{
                        margin: '1px 0',
                        animationDelay: `${i * 60}ms`,
                        borderRadius: 'var(--radius-md)',
                        opacity: 0.55,
                        pointerEvents: 'none',
                      }}
                      aria-hidden="true"
                    />
                  </li>
                ))}
              </ul>
            ) : (
              <ul role="list">
                {navItems
                  .filter(({ to, anyOf }) =>
                    anyOf ? anyOf.some((id) => allowed(id)) : allowed(to.replace(/^\//, '')))
                  .map(({ to, label, icon, showsPendingBadge }) => (
                    <li key={to}>
                      <NavLink
                        to={to}
                        className={({ isActive }) =>
                          ['nav-link', isActive ? 'active' : ''].filter(Boolean).join(' ')
                        }
                      >
                        {icon}
                        {label}
                        {showsPendingBadge && pendingCount > 0 && (
                          <span
                            className="badge-count"
                            role="status"
                            aria-label={t('nav.pending_aria').replace('{count}', String(pendingCount))}
                          >
                            {pendingCount}
                          </span>
                        )}
                      </NavLink>
                    </li>
                  ))}
                <AdsNavItem availability={adsAvailability} />
              </ul>
            )}
          </div>
          <RecentsSection activeConvId={chat.convId} conversationsTick={chat.conversationsTick}
            loadConversation={handleLoadConversation} startNew={handleNewChat} />
        </div>

        {/* Language selector + user chip */}
        <div className="sidebar-user">
          <div className="user-avatar" aria-hidden="true">U</div>
          <span className="sidebar-user-name">Safent</span>
          <div className="sidebar-lang" role="group" aria-label={t('settings.language')}>
            <button
              type="button"
              className={`sidebar-lang-btn${locale === 'es' ? ' sidebar-lang-btn--active' : ''}`}
              onClick={() => setLocale('es')}
              aria-pressed={locale === 'es'}
              title={t('settings.lang.es')}
            >
              ES
            </button>
            <button
              type="button"
              className={`sidebar-lang-btn${locale === 'en' ? ' sidebar-lang-btn--active' : ''}`}
              onClick={() => setLocale('en')}
              aria-pressed={locale === 'en'}
              title={t('settings.lang.en')}
            >
              EN
            </button>
          </div>
        </div>

        <SystemUpdateFooter />
      </nav>

      <main className="main-content" id="main-content" tabIndex={-1}>
        {/* Freno de emergencia (025 Top-KILL) — visible on EVERY view, not just Seguridad. */}
        <KillSwitchBanner />
        {/* Pass the shared chat state down to ChatView via outlet context */}
        <AdsWorkspaceContext.Provider value={{ setPanelActive: setAdsPanelActive, returnToSafent }}>
        <Outlet context={{
          draft,
          convId: chat.convId,
          agentId: chat.agentId,
          agentName: boundAgentName,
          loadConversation: handleLoadConversation,
          startNew: handleNewChat,
          startNewWithAgent: handleStartNewWithAgent,
          sendMessage: handleSendMessage,
          messages: chat.messages,
          status: chat.status,
          stopStream: chat.stopStream,
          approvalRefreshTick,
          conversationsTick: chat.conversationsTick,
          reloadProvider: activeProviderReload,
          reconnecting: chat.reconnecting,
          streamError: chat.streamError,
          cancellation: chat.cancellation,
          liveBrowserActive: chat.liveBrowserActive,
        } satisfies ChatOutletContext} />
        </AdsWorkspaceContext.Provider>
      </main>
    </div>
  )
}
