import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { Settings } from 'lucide-react'
import { listConversations, listPendingApprovals } from '../api/client'
import { useChat } from '../hooks/useChat'
import { useFeatures } from '../hooks/useFeatures'
import type { ConversationSummary } from '../api/types'
import NotificationsPanel from './NotificationsPanel'
import { useT, useLocale } from '../lib/i18n'

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

function TasksIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="12" height="12" rx="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5 8l2 2 4-4" stroke="currentColor" strokeWidth="1.4"
        strokeLinecap="round" strokeLinejoin="round" />
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

function SkillsIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <polygon points="8,1 10,6 15,6 11,9 13,14 8,11 3,14 5,9 1,6 6,6"
        stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

function IntegrationsIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="4" cy="8" r="2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12" cy="4" r="2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12" cy="12" r="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 8h2M10 5 7 7M10 11 7 9"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

function McpIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2v3M8 11v3M2 8h3M11 8h3"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="8" cy="8" r="2.5" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

function ProvidersIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <ellipse cx="8" cy="4.5" rx="5.5" ry="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M2.5 4.5v7c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2v-7"
        stroke="currentColor" strokeWidth="1.4" />
      <path d="M2.5 8c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2"
        stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

function SecurityIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2L3 4v4c0 3.3 2.3 5.6 5 6.4C11.7 13.6 14 11.3 14 8V4L8 2Z"
        stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

function MemoriaIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2a4 4 0 0 1 4 4c0 1.2-.4 2.4-1.2 3.2L8 14l-2.8-4.8A4 4 0 0 1 8 2Z"
        stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <circle cx="8" cy="6" r="1.5" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}

function ArchivosIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M4 2h5l3 3v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z"
        stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M9 2v3h3" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

function CosteIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2 12l3.5-4 3 3L11 7l3 3"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 4h12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
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

/** Sidebar núcleo: the only 3 views shown as top-level nav. Everything else lives behind Ajustes. */
function useCoreNavItems(): NavItem[] {
  const t = useT()
  return [
    { to: '/chat',    label: t('nav.chat'),    icon: <ChatIcon /> },
    { to: '/agentes', label: t('nav.agentes'), icon: <AgentsIcon /> },
    { to: '/skills',  label: t('nav.skills'),  icon: <SkillsIcon /> },
  ]
}

/**
 * The 8 config sections shown as tabs inside the Ajustes page, in display order.
 * Exported so AjustesView can reuse the same {to, label} pairs instead of
 * duplicating the nav.* label lookups. "En vivo" is the Ajustes page's 9th tab
 * but isn't a sidebar nav item, so it's added separately in AjustesView.
 */
export function useSettingsNavItems(): NavItem[] {
  const t = useT()
  return [
    { to: '/programadas',   label: t('nav.programadas'),   icon: <TasksIcon /> },
    { to: '/proveedores',   label: t('nav.proveedores'),   icon: <ProvidersIcon /> },
    { to: '/integraciones', label: t('nav.integraciones'), icon: <IntegrationsIcon /> },
    { to: '/mcp',           label: t('nav.mcp'),           icon: <McpIcon /> },
    { to: '/archivos',      label: t('nav.archivos'),      icon: <ArchivosIcon /> },
    { to: '/seguridad',     label: t('nav.seguridad'),     icon: <SecurityIcon /> },
    { to: '/memoria',       label: t('nav.memoria'),       icon: <MemoriaIcon /> },
    { to: '/coste',         label: t('nav.coste'),         icon: <CosteIcon /> },
  ]
}

// ── Recientes ─────────────────────────────────────────────────────────────────

const PREVIEW_COUNT = 3

function relativeTime(iso?: string): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'Ahora'
  if (mins < 60) return `Hace ${mins} min`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `Hace ${hrs} h`
  return `Hace ${Math.floor(hrs / 24)} d`
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
  /** Call after a provider is connected/activated to trigger an immediate re-check of the nudge state. */
  reloadProvider(): void
  /** True while re-attaching to a stream that was in-flight before a page refresh. */
  reconnecting: boolean
  /** Sticky: the in-flight turn's task is using the browser → chat can show live view. */
  liveBrowserActive: boolean
}

interface RecentsSectionProps {
  activeConvId: string | null
  loadConversation(id: string): Promise<void>
}

function RecentsSection({ activeConvId, loadConversation }: RecentsSectionProps) {
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

  // Re-load when the active conversation changes (new conversation started)
  useEffect(() => {
    if (hasMounted.current) load()
  }, [activeConvId, load])

  async function handleSelect(id: string) {
    navigate('/chat')
    await loadConversation(id)
  }

  const visible = expanded ? conversations : conversations.slice(0, PREVIEW_COUNT)
  const overflow = conversations.length - PREVIEW_COUNT

  if (loading) {
    return (
      <div className="sidebar-recents" aria-label="Conversaciones recientes">
        <div className="sidebar-section-label">Recientes</div>
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
      <div className="sidebar-recents" aria-label="Conversaciones recientes">
        <div className="sidebar-section-label">Recientes</div>
        <p className="recent-empty">Sin conversaciones recientes</p>
      </div>
    )
  }

  return (
    <div className="sidebar-recents" aria-label="Conversaciones recientes">
      <div className="sidebar-section-label">Recientes</div>
      <ul role="listbox" aria-label="Conversaciones recientes">
        {visible.map(c => {
          const id = (c as ConversationSummary & { conversation_id?: string }).conversation_id ?? c.id
          if (!id) return null
          const title = c.title ?? 'Sin título'
          const time = relativeTime(
            (c as ConversationSummary & { last_msg_at?: string }).last_msg_at
            ?? c.updated_at
            ?? c.created_at
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
              {expanded ? 'Ver menos' : `Cargar más (${overflow})`}
            </button>
          </li>
        )}
      </ul>
    </div>
  )
}

export default function Layout({ activeProviderReload }: LayoutProps) {
  const navigate = useNavigate()
  const coreNavItems = useCoreNavItems()
  const t = useT()
  const { locale, setLocale } = useLocale()
  const { isLoading: featuresLoading, allowed } = useFeatures()

  // Strip the leading slash to get the view identifier (e.g. '/proveedores' → 'proveedores').
  // 'chat' is always forced visible even if the backend omits it (defensive).
  const navItems = featuresLoading
    ? [] // render skeleton instead — see below
    : coreNavItems.filter(({ to }) => {
        const viewId = to.replace(/^\//, '')
        return allowed(viewId)
      })
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

  // Global pending-approvals count → badge on the Seguridad nav. HITL cards from
  // NON-chat cycles (scheduled / autonomous tasks; conversation_id=null) don't
  // anchor to a chat thread, so without this they'd be invisible outside the
  // Security view. The badge guarantees the owner always sees there's something
  // waiting to approve.
  const [pendingCount, setPendingCount] = useState(0)
  useEffect(() => {
    let alive = true
    const poll = () => {
      listPendingApprovals()
        .then(a => { if (alive) setPendingCount(Array.isArray(a) ? a.length : 0) })
        .catch(() => { /* transient — keep last known count */ })
    }
    poll()
    const id = setInterval(poll, 6000)
    return () => { alive = false; clearInterval(id) }
  }, [approvalRefreshTick])

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
      <nav className="sidebar" aria-label="Navegación principal">
        {/* Wordmark */}
        <div className="sidebar-wordmark">
          <div className="sidebar-wordmark-inner">
            <div className="sidebar-mark" aria-hidden="true">L</div>
            <span className="sidebar-name">Lumen</span>
          </div>
          <NotificationsPanel loadConversation={chat.loadConversation} />
        </div>

        {/* New chat button — always resets the conversation */}
        <button
          className="sidebar-new-chat"
          aria-label="Nuevo chat"
          type="button"
          onClick={handleNewChat}
        >
          <PlusIcon />
          Nuevo chat
        </button>

        {/* Scrollable area */}
        <div className="sidebar-scroll">
          {/* Recientes — reads activeConvId directly from the lifted chat state */}
          <RecentsSection
            activeConvId={chat.convId}
            loadConversation={chat.loadConversation}
          />

          {/* Main nav */}
          <div className="sidebar-nav">
            <div className="sidebar-section-label">Navegación</div>
            {featuresLoading ? (
              // Mirror the real nav: fixed count = no layout shift on load.
              <ul role="list" aria-busy="true" aria-label="Cargando navegación">
                {Array.from({ length: 3 }, (_, i) => (
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
                {navItems.map(({ to, label, icon }) => (
                  <li key={to}>
                    <NavLink
                      to={to}
                      className={({ isActive }) =>
                        ['nav-link', isActive ? 'active' : ''].filter(Boolean).join(' ')
                      }
                      aria-current={undefined}
                    >
                      {icon}
                      {label}
                    </NavLink>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Ajustes — every other section (programadas, modelo de IA, integraciones,
            herramientas, archivos, seguridad, memoria, coste, en vivo) lives behind
            this single entry as tabs. The pending-approvals badge lives here too,
            since Seguridad is now one tab among the rest. */}
        <div
          className="sidebar-nav"
          style={{ borderTop: '1px solid var(--color-border-subtle)', paddingTop: 'var(--space-2)' }}
        >
          <ul role="list">
            <li>
              <NavLink
                to="/ajustes"
                className={({ isActive }) =>
                  ['nav-link', isActive ? 'active' : ''].filter(Boolean).join(' ')
                }
              >
                <Settings className="nav-icon" aria-hidden="true" />
                {t('nav.ajustes')}
                {pendingCount > 0 && (
                  <span
                    className="badge-count"
                    role="status"
                    aria-label={t('nav.ajustes.pending_aria').replace('{count}', String(pendingCount))}
                  >
                    {pendingCount}
                  </span>
                )}
              </NavLink>
            </li>
          </ul>
        </div>

        {/* Language selector + user chip */}
        <div className="sidebar-user">
          <div className="user-avatar" aria-hidden="true">U</div>
          <span className="sidebar-user-name">Lumen</span>
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
          reloadProvider: activeProviderReload,
          reconnecting: chat.reconnecting,
          liveBrowserActive: chat.liveBrowserActive,
        } satisfies ChatOutletContext} />
      </main>
    </div>
  )
}
