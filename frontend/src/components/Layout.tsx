import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { sileo } from 'sileo'
import { RefreshCw, Trash2 } from 'lucide-react'
import {
  listConversations,
  getSystemUpdate,
  requestSystemUpdate,
  requestSystemUninstall,
  type SystemUpdateStatus,
} from '../api/client'
import { useChat } from '../hooks/useChat'
import { useFeatures } from '../hooks/useFeatures'
import { usePendingApprovals } from '../hooks/usePendingApprovals'
import { usePendingInboundDelegations } from '../hooks/usePendingInboundDelegations'
import type { ConversationSummary } from '../api/types'
import NotificationsPanel from './NotificationsPanel'
import { useConfirmDialog } from './ConfirmDialog'
import { useT, useLocale } from '../lib/i18n'
import { CAPACIDADES_VIEW_IDS, SISTEMA_VIEW_IDS } from '../views/SectionHubs'

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

function AgentsIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="5.5" r="2.5" stroke="currentColor" strokeWidth="1.4" />
      <path d="M2 14c0-3 2.686-4.5 6-4.5S14 11 14 14"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

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
  anyOf?: string[]
  /** Show the pending-approvals badge on this item. */
  showsPendingBadge?: boolean
}

/**
 * Four clean entries (owner decision): Chat · Agentes · Capacidades · Sistema.
 * The two hubs contain every other section as tabs (see SectionHubs.tsx).
 */
function useNavItems(): HubNavItem[] {
  const t = useT()
  return [
    { to: '/chat',        label: t('nav.chat'),                 icon: <ChatIcon /> },
    { to: '/agentes',     label: t('nav.agentes'),              icon: <AgentsIcon /> },
    { to: '/capacidades', label: t('nav.section.capabilities'), icon: <CapacidadesIcon />, anyOf: CAPACIDADES_VIEW_IDS },
    { to: '/sistema',     label: t('nav.section.system'),       icon: <SistemaIcon />, anyOf: SISTEMA_VIEW_IDS, showsPendingBadge: true },
  ]
}

// ── System update ─────────────────────────────────────────────────────────────

const SYSTEM_UPDATE_POLL_MS = 15 * 60_000
// While an update is in flight, poll fast: the owner is WATCHING "Updating…"
// and must see completion (or the stale-flag expiry) in seconds, not in 15 min.
const SYSTEM_UPDATE_ACTIVE_POLL_MS = 20_000

/** Compare two dotted versions ("0.8.34"). >0 if a is newer than b, 0 if equal, <0 older. */
function cmpVersion(a: string, b: string): number {
  const pa = a.split('.').map(n => parseInt(n, 10) || 0)
  const pb = b.split('.').map(n => parseInt(n, 10) || 0)
  const len = Math.max(pa.length, pb.length)
  for (let i = 0; i < len; i++) {
    const d = (pa[i] || 0) - (pb[i] || 0)
    if (d !== 0) return d > 0 ? 1 : -1
  }
  return 0
}

/** The Tauri desktop shell (host, has internet) injects the latest version it fetched from
 *  the published VERSION file. The sandboxed backend's own check can be blocked by the
 *  egress cage, so we trust whichever source says "newer". */
function injectedLatestVersion(): string {
  if (typeof window === 'undefined') return ''
  const v = (window as unknown as { __safentLatestVersion?: unknown }).__safentLatestVersion
  return typeof v === 'string' ? v.trim() : ''
}

/** Footer: current version, a calm "Actualizar" affordance that ALERTS (pulsing accent +
 *  latest version) when a newer build exists, and a compact "Desinstalar". Icon-forward
 *  (Lucide) and stacked into two rows so nothing overflows the narrow sidebar. The refresh
 *  icon spins while an update is running. */
function SystemUpdateFooter() {
  const t = useT()
  const [status, setStatus] = useState<SystemUpdateStatus | null>(null)
  const [confirmUpdate, confirmUpdateDialog] = useConfirmDialog()

  const poll = useCallback(() => {
    getSystemUpdate().then(setStatus)
  }, [])

  const updating = !!status?.updating
  useEffect(() => {
    poll()
    const id = setInterval(poll, updating ? SYSTEM_UPDATE_ACTIVE_POLL_MS : SYSTEM_UPDATE_POLL_MS)
    return () => clearInterval(id)
  }, [poll, updating])

  async function handleUpdateClick() {
    const ok = await confirmUpdate({
      title: t('sysupdate.confirm.title'),
      description: t('sysupdate.confirm.body'),
      confirmLabel: t('sysupdate.confirm.ok'),
    })
    if (!ok) return

    try {
      const res = await requestSystemUpdate()
      setStatus(prev => (prev ? { ...prev, updating: res.updating } : prev))
      sileo.success({ title: t('sysupdate.toast.started') })
    } catch {
      sileo.error({ title: t('sysupdate.err.start') })
    }
  }

  async function handleUninstallClick() {
    const ok = await confirmUpdate({
      title: t('sysuninstall.confirm.title'),
      description: t('sysuninstall.confirm.body'),
      confirmLabel: t('sysuninstall.confirm.ok'),
    })
    if (!ok) return
    try {
      await requestSystemUninstall()
      sileo.success({ title: t('sysuninstall.toast.started') })
    } catch {
      sileo.error({ title: t('sysuninstall.err.start') })
    }
  }

  if (!status?.current_version) return null

  const latest = status.latest_version || injectedLatestVersion()
  const available = !updating && (
    !!status.update_available || (!!latest && cmpVersion(latest, status.current_version) > 0)
  )
  const availableLabel = latest ? `${t('sysupdate.available')} · v${latest}` : t('sysupdate.available')

  return (
    <div
      style={{
        display: 'flex', flexDirection: 'column', gap: 'var(--space-1)',
        padding: `var(--space-2) var(--space-4) var(--space-3)`,
        fontSize: 'var(--text-xs)', color: 'var(--color-text-dim)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', minWidth: 0 }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {t('sysupdate.current').replace('{v}', status.current_version)}
        </span>
        <span style={{ flex: 1 }} />
        {available && (
          <span
            title={availableLabel}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: 'var(--color-accent)', whiteSpace: 'nowrap', maxWidth: '55%' }}
          >
            <span aria-hidden="true" style={{
              width: 6, height: 6, borderRadius: '50%', background: 'var(--color-accent)',
              animation: 'pulse-dot 1.6s ease-in-out infinite', flex: '0 0 auto',
            }} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {latest ? `v${latest}` : t('sysupdate.available')}
            </span>
          </span>
        )}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
        <button
          type="button"
          className="cv-btn cv-btn--ghost cv-btn--sm"
          style={{
            height: 'auto', padding: `3px var(--space-2)`, fontSize: 'var(--text-xs)',
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
            flex: 1, minWidth: 0,
            ...(available ? {
              color: 'var(--color-accent)',
              borderColor: 'color-mix(in srgb, var(--color-accent) 45%, transparent)',
            } : {}),
          }}
          onClick={handleUpdateClick}
          disabled={updating}
          title={available ? availableLabel : undefined}
          aria-label={available ? `${availableLabel} — ${t('sysupdate.action')}` : t('sysupdate.action')}
        >
          <RefreshCw size={13} className={updating ? 'spin' : undefined} aria-hidden="true" style={{ flex: '0 0 auto' }} />
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {updating ? t('sysupdate.updating') : t('sysupdate.action')}
          </span>
        </button>
        <button
          type="button"
          className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
          style={{
            height: 'auto', padding: `3px 7px`,
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto',
          }}
          onClick={handleUninstallClick}
          disabled={updating}
          title={t('sysuninstall.action')}
          aria-label={t('sysuninstall.action')}
        >
          <Trash2 size={13} aria-hidden="true" />
        </button>
      </div>
      {confirmUpdateDialog}
    </div>
  )
}

// ── Recientes ─────────────────────────────────────────────────────────────────

const PREVIEW_COUNT = 3

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

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n) + '…' : s
}

// ── ChatOutletContext — shared between RecentsSection (in nav) and ChatView ──

export interface ChatOutletContext {
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
  /** Sticky: the in-flight turn's task is using the browser → chat can show live view. */
  liveBrowserActive: boolean
}

interface RecentsSectionProps {
  activeConvId: string | null
  conversationsTick: number
  loadConversation(id: string): Promise<void>
}

function RecentsSection({ activeConvId, conversationsTick, loadConversation }: RecentsSectionProps) {
  const t = useT()
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const hasMounted = useRef(false)

  const load = useCallback(() => {
    listConversations()
      .then(data => {
        setConversations(Array.isArray(data) ? data : [])
        setLoading(false)
      })
      .catch(() => { setLoading(false) })
  }, [])

  useEffect(() => {
    if (!hasMounted.current) {
      hasMounted.current = true
      load()
    }
  }, [load])

  // Re-load when the active conversation changes (new conversation started) AND when a
  // turn finishes (conversationsTick) — the latter is when a brand-new chat is finally
  // persisted to the mirror, so this is what makes it appear without a full reload.
  useEffect(() => {
    if (hasMounted.current) load()
  }, [activeConvId, conversationsTick, load])

  async function handleSelect(id: string) {
    navigate('/chat')
    await loadConversation(id)
  }

  const visible = expanded ? conversations : conversations.slice(0, PREVIEW_COUNT)
  const overflow = conversations.length - PREVIEW_COUNT

  if (loading) {
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

  if (conversations.length === 0) {
    return (
      <div className="sidebar-recents" aria-label={t('layout.recents.aria')}>
        <div className="sidebar-section-label">{t('layout.recents.label')}</div>
        <p className="recent-empty">{t('layout.recents.empty')}</p>
      </div>
    )
  }

  return (
    <div className="sidebar-recents" aria-label={t('layout.recents.aria')}>
      <div className="sidebar-section-label">{t('layout.recents.label')}</div>
      <ul role="listbox" aria-label={t('layout.recents.aria')}>
        {visible.map(c => {
          const id = (c as ConversationSummary & { conversation_id?: string }).conversation_id ?? c.id
          if (!id) return null
          const title = c.title ?? t('layout.recents.untitled')
          const time = relativeTime(
            (c as ConversationSummary & { last_msg_at?: string }).last_msg_at
            ?? c.updated_at
            ?? c.created_at,
            t,
          )
          const isActive = id === activeConvId

          return (
            <li key={id} role="option" aria-selected={isActive}>
              <button
                className={`recent-item${isActive ? ' recent-item--active' : ''}`}
                onClick={() => handleSelect(id)}
                type="button"
                title={title}
              >
                <span className="recent-title">{truncate(title, 38)}</span>
                {time && <span className="recent-time">{time}</span>}
              </button>
            </li>
          )
        })}
        {overflow > 0 && (
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
      </ul>
    </div>
  )
}

export default function Layout({ activeProviderReload }: LayoutProps) {
  const navigate = useNavigate()
  const navItems = useNavItems()
  const t = useT()
  const { locale, setLocale } = useLocale()
  const { isLoading: featuresLoading, allowed } = useFeatures()
  // activeProviderReload is exposed on the outlet context so views like
  // ProvidersView can signal an immediate re-check after connecting a model.
  // The hook already self-heals via a 5 s poll; this enables instant feedback.

  // Chat state lives here, above both the sidebar nav (RecentsSection) and
  // the main content area (ChatView). ChatView receives it via outlet context.
  const chat = useChat()
  // Display name for the agent bound to the current chat (cleared on new chat).
  const [boundAgentName, setBoundAgentName] = useState<string | null>(null)

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
    usePendingApprovals(6000, approvalRefreshTick).length +
    usePendingInboundDelegations(6000, approvalRefreshTick).length

  async function handleSendMessage(text: string) {
    await chat.sendMessage(text)
    setApprovalRefreshTick(t => t + 1)
  }

  function handleNewChat() {
    chat.startNew()
    setBoundAgentName(null)
    navigate('/chat')
  }

  function handleStartNewWithAgent(agentId: string, agentName: string) {
    chat.startNewWithAgent(agentId)
    setBoundAgentName(agentName)
    navigate('/chat')
  }

  return (
    <div className="app-shell">
      <nav className="sidebar" aria-label={t('layout.nav.aria')}>
        {/* Wordmark */}
        <div className="sidebar-wordmark">
          <div className="sidebar-wordmark-inner">
            <div className="sidebar-mark" aria-hidden="true">L</div>
            <span className="sidebar-name">Safent</span>
          </div>
          <NotificationsPanel loadConversation={chat.loadConversation} />
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
          {/* Recientes — reads activeConvId directly from the lifted chat state */}
          <RecentsSection
            activeConvId={chat.convId}
            conversationsTick={chat.conversationsTick}
            loadConversation={chat.loadConversation}
          />

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
              </ul>
            )}
          </div>
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

      <main className="main-content page-enter" id="main-content" tabIndex={-1}>
        {/* Pass the shared chat state down to ChatView via outlet context */}
        <Outlet context={{
          convId: chat.convId,
          agentId: chat.agentId,
          agentName: boundAgentName,
          loadConversation: chat.loadConversation,
          startNew: chat.startNew,
          startNewWithAgent: handleStartNewWithAgent,
          sendMessage: handleSendMessage,
          messages: chat.messages,
          status: chat.status,
          stopStream: chat.stopStream,
          approvalRefreshTick,
          conversationsTick: chat.conversationsTick,
          reloadProvider: activeProviderReload,
          reconnecting: chat.reconnecting,
          liveBrowserActive: chat.liveBrowserActive,
        } satisfies ChatOutletContext} />
      </main>
    </div>
  )
}
