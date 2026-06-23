import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { listConversations } from '../api/client'
import { useChat } from '../hooks/useChat'
import type { ConversationSummary } from '../api/types'

// Layout now receives the provider-check state from the gate in App.tsx
// so it can show the "Falta conectar un modelo" badge without a second fetch.
export interface LayoutProps {
  hasActiveProvider: boolean
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

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

const NAV_ITEMS: NavItem[] = [
  { to: '/chat',         label: 'Chat',            icon: <ChatIcon /> },
  { to: '/programadas',  label: 'Programadas',     icon: <TasksIcon /> },
  { to: '/agentes',      label: 'Agentes',         icon: <AgentsIcon /> },
  { to: '/skills',       label: 'Habilidades',     icon: <SkillsIcon /> },
  { to: '/integraciones',label: 'Integraciones',   icon: <IntegrationsIcon /> },
  { to: '/mcp',          label: 'Herramientas',    icon: <McpIcon /> },
  { to: '/proveedores',  label: 'Proveedores',     icon: <ProvidersIcon /> },
  { to: '/seguridad',    label: 'Seguridad',       icon: <SecurityIcon /> },
  { to: '/memoria',      label: 'Memoria',         icon: <MemoriaIcon /> },
]

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
  loadConversation(id: string): Promise<void>
  startNew(): void
  sendMessage(text: string): Promise<void>
  messages: ReturnType<typeof useChat>['messages']
  status: ReturnType<typeof useChat>['status']
  stopStream(): void
  /** Incremented each time the user sends a message — signals PendingApprovalsInChat to poll immediately. */
  approvalRefreshTick: number
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
            className="recent-item"
            style={{ animation: `shimmer 1.4s ${i * 80}ms infinite linear` }}
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
              className="recent-item"
              style={{ color: 'var(--accent)', fontWeight: 500 }}
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

export default function Layout({ hasActiveProvider, activeProviderReload }: LayoutProps) {
  const navigate = useNavigate()
  // Silence the unused-var lint for activeProviderReload until a future
  // feature (auto-reconnect after provider change) uses it.
  void activeProviderReload

  // Chat state lives here, above both the sidebar nav (RecentsSection) and
  // the main content area (ChatView). ChatView receives it via outlet context.
  const chat = useChat()

  // Bumped each time the user sends a message so PendingApprovalsInChat can
  // fire an immediate poll without waiting for the 3 s interval.
  const [approvalRefreshTick, setApprovalRefreshTick] = useState(0)

  async function handleSendMessage(text: string) {
    await chat.sendMessage(text)
    setApprovalRefreshTick(t => t + 1)
  }

  function handleNewChat() {
    chat.startNew()
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
            <ul role="list">
              {NAV_ITEMS.map(({ to, label, icon }) => (
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
          </div>
        </div>

        {/* "Connect a model" nudge badge — visible only when no provider is active */}
        {!hasActiveProvider && (
          <NavLink
            to="/bienvenida"
            className="sidebar-setup-badge"
            aria-label="Conecta un modelo para usar el chat"
          >
            <span className="sidebar-setup-badge__dot" aria-hidden="true" />
            <span className="sidebar-setup-badge__text">Falta conectar un modelo</span>
            <span className="sidebar-setup-badge__arrow" aria-hidden="true">→</span>
          </NavLink>
        )}

        {/* User chip */}
        <div className="sidebar-user">
          <div className="user-avatar" aria-hidden="true">U</div>
          <span className="sidebar-user-name">Lumen</span>
        </div>
      </nav>

      <main className="main-content" id="main-content" tabIndex={-1}>
        {/* Pass the shared chat state down to ChatView via outlet context */}
        <Outlet context={{
          convId: chat.convId,
          loadConversation: chat.loadConversation,
          startNew: chat.startNew,
          sendMessage: handleSendMessage,
          messages: chat.messages,
          status: chat.status,
          stopStream: chat.stopStream,
          approvalRefreshTick,
        } satisfies ChatOutletContext} />
      </main>
    </div>
  )
}
