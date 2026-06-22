/**
 * ChatView — streaming chat with the Lumen agent.
 *
 * Chat state is owned by Layout and passed down via outlet context so that
 * RecentsSection (in the sidebar) and ChatView share the same instance.
 * This file is purely presentational — all state mutations go through
 * the outlet context.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ChangeEvent,
} from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import type { ChatMessage, ToolStep } from '../hooks/useChat'
import { listProviders, uploadWorkspaceFile, ApiError } from '../api/client'
import type { Provider } from '../api/types'
import type { ChatOutletContext } from '../components/Layout'
import ContextPanel from '../components/ContextPanel'

// ── i18n strings (ES, matching vanilla i18n.js) ───────────────────────────────

const STRINGS = {
  welcomeTitle:  'Hola, soy Lumen',
  welcomeSubtitle: 'Tu agente de trabajo personal. Dime en qué puedo ayudarte hoy.',
  suggest1: 'Investiga los mejores CRMs para una startup B2B',
  suggest2: 'Redacta un email de propuesta comercial',
  suggest3: 'Organiza mis tareas de esta semana en un plan de acción',
  suggest4: 'Analiza este documento y extrae los puntos clave',
  placeholder: 'Escribe a Lumen…',
  send: 'Enviar',
  stop: 'Detener',
  disclaimer: 'Lumen es IA y puede cometer errores. Verifica las respuestas importantes.',
}

const SUGGESTIONS = [
  STRINGS.suggest1,
  STRINGS.suggest2,
  STRINGS.suggest3,
  STRINGS.suggest4,
]

// ── Welcome screen ────────────────────────────────────────────────────────────

interface WelcomeProps {
  onSuggestion(text: string): void
}

function Welcome({ onSuggestion }: WelcomeProps) {
  return (
    <div className="chat-welcome" role="main">
      <div className="welcome-mark" aria-hidden="true">L</div>
      <h1 className="welcome-title">{STRINGS.welcomeTitle}</h1>
      <p className="welcome-subtitle">{STRINGS.welcomeSubtitle}</p>
      <div className="welcome-suggestions" role="list" aria-label="Sugerencias">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            className="suggestion-pill"
            role="listitem"
            onClick={() => onSuggestion(s)}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Tool summary block ────────────────────────────────────────────────────────

interface ToolSummaryProps {
  steps: ToolStep[]
  isStreaming: boolean
}

function ToolSummary({ steps, isStreaming }: ToolSummaryProps) {
  if (steps.length === 0) return null
  const count = steps.length
  const last = steps[steps.length - 1]
  const label = isStreaming
    ? `${last.label}${last.target ? ` — ${last.target.slice(0, 48)}` : ''}`
    : `Usó ${count} herramienta${count > 1 ? 's' : ''}`

  return (
    <details className="tool-summary-group">
      <summary className="tool-summary-group__summary">
        <span className="tool-summary-group__label">{label}</span>
        {!isStreaming && (
          <span className="tool-summary-group__count" aria-label={`${count} herramientas`}>
            {count}
          </span>
        )}
        <span className="tool-summary-group__chevron" aria-hidden="true">
          <ChevronIcon />
        </span>
      </summary>
      <div className="tool-summary-group__body">
        {steps.map((step, i) => (
          <div key={i} className="tool-step-item">
            <span className="tool-step-item__label">{step.label}</span>
            {step.target && (
              <span className="tool-step-item__target">{step.target}</span>
            )}
          </div>
        ))}
      </div>
    </details>
  )
}

// ── Thinking block ────────────────────────────────────────────────────────────

interface ThinkingBlockProps {
  text: string
  done: boolean
}

function ThinkingBlock({ text, done }: ThinkingBlockProps) {
  if (!text) return null
  return (
    <details className="thinking-block">
      <summary className="thinking-block__summary">
        <span className="thinking-block__label">
          {done ? 'Proceso de pensamiento' : 'Pensando…'}
        </span>
        <span className="thinking-block__chevron" aria-hidden="true">
          <ChevronIcon />
        </span>
      </summary>
      <div className="thinking-block__body">{text}</div>
    </details>
  )
}

// ── Message bubbles ───────────────────────────────────────────────────────────

interface UserMessageProps {
  text: string
  failed?: boolean
}

function UserMessage({ text, failed }: UserMessageProps) {
  return (
    <div
      className={`message message--user${failed ? ' message--failed' : ''}`}
      role="article"
      aria-label="Tu mensaje"
    >
      <div className="message__bubble">{text}</div>
      {failed && (
        <p
          style={{ fontSize: 'var(--text-caption)', color: 'var(--danger)', margin: '4px 0 0' }}
          role="alert"
        >
          No se pudo enviar
        </p>
      )}
    </div>
  )
}

interface AssistantMessageProps {
  message: Extract<ChatMessage, { type: 'assistant' }>
}

function AssistantMessage({ message }: AssistantMessageProps) {
  const { thinkingText, thinkingDone, toolSteps, activityText, renderedHtml, isStreaming } = message

  return (
    <div className="message message--agent" role="article" aria-label="Respuesta de Lumen">
      <ThinkingBlock text={thinkingText} done={thinkingDone} />
      <ToolSummary steps={toolSteps} isStreaming={isStreaming} />

      {/* Live activity excerpt while streaming */}
      {isStreaming && activityText && (
        <div className="agent-activity" aria-live="polite" aria-atomic="false">
          {lastLine(activityText)}
        </div>
      )}

      {/* Final rendered markdown (shown after stream completes) */}
      {!isStreaming && renderedHtml && (
        <div
          className="agent-prose"
          /* Safe: renderedHtml is produced by DOMPurify.sanitize — see lib/markdown.ts */
          dangerouslySetInnerHTML={{ __html: renderedHtml }}
        />
      )}

      {/* Streaming cursor */}
      {isStreaming && !activityText && (
        <div className="agent-activity" aria-live="polite">
          <span className="spin" aria-hidden="true">⟳</span>
        </div>
      )}
    </div>
  )
}

function lastLine(text: string): string {
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean)
  return lines.length ? `· ${lines[lines.length - 1]}` : ''
}

// ── Status bar ────────────────────────────────────────────────────────────────

interface StatusBarProps {
  phase: string
  text?: string
}

function StatusBar({ phase, text }: StatusBarProps) {
  if (phase === 'idle') return null
  const isError = phase === 'error'

  return (
    <div
      className={`chat-status${isError ? ' chat-status--error' : ''}`}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
    >
      {!isError && <SpinnerIcon />}
      <span>{text}</span>
    </div>
  )
}

// ── No-model CTA banner ───────────────────────────────────────────────────────

function NoModelBanner() {
  const navigate = useNavigate()
  return (
    <div
      className="chat-status chat-status--error"
      role="alert"
      style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 'var(--sp-3)' }}
    >
      <span>Sin modelo configurado. Conecta un proveedor para empezar a chatear.</span>
      <button
        type="button"
        className="cv-btn cv-btn--primary cv-btn--sm"
        onClick={() => navigate('/proveedores')}
      >
        Ir a Proveedores
      </button>
    </div>
  )
}

// ── Model picker ──────────────────────────────────────────────────────────────

function useActiveProvider() {
  const [provider, setProvider] = useState<Provider | null>(null)

  useEffect(() => {
    listProviders()
      .then(data => {
        const arr = Array.isArray(data) ? data : []
        setProvider(arr.find(p => p.is_active) ?? arr[0] ?? null)
      })
      .catch(() => setProvider(null))
  }, [])

  return provider
}

function ModelPicker() {
  const navigate = useNavigate()
  const provider = useActiveProvider()

  const label = provider
    ? (provider.default_model ?? provider.alias ?? provider.name ?? 'Modelo activo')
    : 'Sin modelo configurado'

  return (
    <button
      className="composer-model-picker"
      onClick={() => navigate('/proveedores')}
      title={provider ? `Proveedor: ${provider.alias ?? provider.name}` : 'Configura un modelo en Proveedores'}
      type="button"
      aria-label={provider ? `Modelo activo: ${label}. Ir a Proveedores` : 'Sin modelo. Ir a Proveedores'}
    >
      <span className="composer-model-picker__label">{label}</span>
      <ChevronIcon />
    </button>
  )
}

// ── Attachment chip ───────────────────────────────────────────────────────────

interface AttachmentChipProps {
  name: string
  uploading: boolean
  error: boolean
  onRemove: () => void
}

function AttachmentChip({ name, uploading, error, onRemove }: AttachmentChipProps) {
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 'var(--sp-2)',
        background: error
          ? 'color-mix(in srgb, var(--danger) 12%, transparent)'
          : 'var(--card2)',
        border: `1px solid ${error ? 'var(--danger)' : 'var(--line2)'}`,
        borderRadius: 'var(--r-sm)',
        padding: '2px var(--sp-2)',
        fontSize: 'var(--text-caption)',
        color: error ? 'var(--danger)' : 'var(--ink2)',
        maxWidth: 200,
      }}
    >
      {uploading ? (
        <span className="spin" aria-hidden="true" style={{ fontSize: 10 }}>⟳</span>
      ) : (
        <span aria-hidden="true" style={{ fontSize: 11 }}>{error ? '⚠' : '📄'}</span>
      )}
      <span
        style={{
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          flex: 1,
          minWidth: 0,
        }}
        title={name}
      >
        {name}
      </span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Quitar adjunto ${name}`}
        style={{
          border: 'none',
          background: 'transparent',
          color: 'inherit',
          cursor: 'pointer',
          padding: 0,
          lineHeight: 1,
          fontSize: 11,
          opacity: 0.7,
          flexShrink: 0,
        }}
        disabled={uploading}
      >
        ✕
      </button>
    </div>
  )
}

// ── Composer ──────────────────────────────────────────────────────────────────

interface PendingAttachment {
  id: string
  file: File
  uploading: boolean
  uploadedPath: string | null
  error: boolean
}

interface ComposerProps {
  disabled: boolean
  isStreaming: boolean
  onSend(text: string): void
  onStop(): void
  value: string
  onChange(v: string): void
}

function Composer({ disabled, isStreaming, onSend, onStop, value, onChange }: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])

  // Auto-grow textarea
  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`
  }, [value])

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!disabled && (value.trim() || attachments.some((a) => a.uploadedPath))) {
        handleSend()
      }
    }
  }

  function handleChange(e: ChangeEvent<HTMLTextAreaElement>) {
    onChange(e.target.value)
  }

  async function handleFileSelect(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? [])
    // Reset input so the same file can be re-attached after removal
    e.target.value = ''
    if (files.length === 0) return

    const newAttachments: PendingAttachment[] = files.map((f) => ({
      id: `${Date.now()}-${Math.random()}`,
      file: f,
      uploading: true,
      uploadedPath: null,
      error: false,
    }))

    setAttachments((prev) => [...prev, ...newAttachments])

    // Upload each file concurrently
    await Promise.all(
      newAttachments.map(async (att) => {
        try {
          const result = await uploadWorkspaceFile(att.file)
          setAttachments((prev) =>
            prev.map((a) =>
              a.id === att.id ? { ...a, uploading: false, uploadedPath: result.path } : a,
            ),
          )
        } catch (err) {
          const msg = err instanceof ApiError ? err.message : 'Error al subir el archivo.'
          console.error(`Attachment upload failed for ${att.file.name}: ${msg}`)
          setAttachments((prev) =>
            prev.map((a) =>
              a.id === att.id ? { ...a, uploading: false, error: true } : a,
            ),
          )
        }
      }),
    )
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((a) => a.id !== id))
  }

  function handleSend() {
    const uploadedPaths = attachments
      .filter((a) => a.uploadedPath !== null)
      .map((a) => a.uploadedPath as string)

    let text = value
    if (uploadedPaths.length > 0) {
      const refs = uploadedPaths.map((p) => `[Adjunto: ${p}]`).join('\n')
      text = text.trim() ? `${text}\n\n${refs}` : refs
    }

    if (text.trim()) {
      onSend(text)
      setAttachments([])
    }
  }

  const anyUploading = attachments.some((a) => a.uploading)
  const canSend = !disabled && !anyUploading && (value.trim() !== '' || attachments.some((a) => a.uploadedPath))

  return (
    <div className="composer-wrap">
      {/* Attachment chips */}
      {attachments.length > 0 && (
        <div
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 'var(--sp-2)',
            paddingBottom: 'var(--sp-2)',
          }}
          aria-label="Archivos adjuntos"
        >
          {attachments.map((att) => (
            <AttachmentChip
              key={att.id}
              name={att.file.name}
              uploading={att.uploading}
              error={att.error}
              onRemove={() => removeAttachment(att.id)}
            />
          ))}
        </div>
      )}

      <div className="composer-box">
        <textarea
          ref={textareaRef}
          className="composer-textarea"
          placeholder={STRINGS.placeholder}
          aria-label="Escribe un mensaje para Lumen"
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
        />
        <div className="composer-toolbar">
          {/* Hidden file input — triggered by the attach button */}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,.pdf,.txt,.md,.docx,.csv"
            multiple
            className="sr-only"
            aria-label="Adjuntar archivo"
            onChange={handleFileSelect}
            tabIndex={-1}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            aria-label="Adjuntar archivo (imágenes, PDF, documentos)"
            title="Adjuntar archivo"
            style={{
              border: 'none',
              background: 'transparent',
              color: 'var(--ink3)',
              cursor: 'pointer',
              padding: 'var(--sp-1)',
              borderRadius: 'var(--r-sm)',
              fontSize: 16,
              lineHeight: 1,
              display: 'flex',
              alignItems: 'center',
              transition: 'color 150ms ease',
            }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = 'var(--ink)' }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = 'var(--ink3)' }}
          >
            <AttachIcon />
          </button>

          <ModelPicker />
          <div className="composer-toolbar-right">
            {isStreaming ? (
              <button
                className="composer-stop"
                onClick={onStop}
                aria-label="Detener generación"
                type="button"
              >
                {STRINGS.stop}
              </button>
            ) : (
              <button
                className="composer-send"
                onClick={handleSend}
                disabled={!canSend}
                aria-label="Enviar mensaje (Enter)"
                aria-busy={anyUploading}
                type="button"
              >
                {anyUploading ? 'Subiendo…' : STRINGS.send}
              </button>
            )}
          </div>
        </div>
      </div>
      <p className="composer-footer">{STRINGS.disclaimer}</p>
    </div>
  )
}

// ── Micro icons ───────────────────────────────────────────────────────────────

function ChevronIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <path d="M4 3l4 3-4 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function AttachIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M13.5 7.5l-6 6a4 4 0 01-5.657-5.657l6-6a2.5 2.5 0 013.535 3.535L5.5 11.25a1 1 0 01-1.414-1.414L10 4"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function PanelToggleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M10 2v12" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

function SpinnerIcon() {
  return (
    <svg className="spin" width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <circle cx="6" cy="6" r="4.5" stroke="currentColor" strokeWidth="1.5" strokeDasharray="14 8" />
    </svg>
  )
}

// ── ChatView ──────────────────────────────────────────────────────────────────

export default function ChatView() {
  // All chat state comes from Layout via outlet context — no duplicate useChat instance.
  const { messages, status, sendMessage, stopStream } = useOutletContext<ChatOutletContext>()
  const [composerText, setComposerText] = useState('')
  const [panelOpen, setPanelOpen] = useState(false)
  const [showNoModel, setShowNoModel] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const userScrolledRef = useRef(false)
  const pinRef = useRef(true)

  const isStreaming = status.phase === 'streaming' || status.phase === 'sending'
  const showWelcome = messages.length === 0

  // Detect no-model 409 error and show the actionable CTA instead of the dead bar
  useEffect(() => {
    if (status.phase === 'error') {
      const msg = (status as { phase: 'error'; message: string }).message ?? ''
      const isNoModel = msg.includes('409') || /sin modelo|no model|no provider|no.*provider/i.test(msg)
      setShowNoModel(isNoModel)
    } else {
      setShowNoModel(false)
    }
  }, [status])

  // Scroll pinning — matches vanilla scrollToBottom logic
  useEffect(() => {
    const el = bodyRef.current
    if (!el) return
    function onScroll() {
      const nearBottom = el!.scrollTop + el!.clientHeight >= el!.scrollHeight - 80
      pinRef.current = nearBottom
      userScrolledRef.current = !nearBottom
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!el) return
    if (!userScrolledRef.current || pinRef.current) {
      el.scrollTop = el.scrollHeight
    }
  })

  const handleSend = useCallback((text: string) => {
    userScrolledRef.current = false
    pinRef.current = true
    setComposerText('')
    setShowNoModel(false)
    void sendMessage(text)
  }, [sendMessage])

  const handleSuggestion = useCallback((text: string) => {
    handleSend(text)
  }, [handleSend])

  const statusText = status.phase === 'streaming' ? status.statusText
    : status.phase === 'sending' ? 'Enviando…'
    : status.phase === 'error' && !showNoModel ? (status as { phase: 'error'; message: string }).message
    : undefined

  return (
    <>
      {/* Outer shell: chat column + optional context panel */}
      <div className="chat-shell">
        <div className="chat-view">
          {/* Topbar */}
          <div className="chat-topbar">
            <span className="chat-topbar-title">
              {showWelcome ? 'Nueva conversación' : 'Chat'}
            </span>
            <button
              className="chat-topbar-panel-btn"
              onClick={() => setPanelOpen(v => !v)}
              aria-pressed={panelOpen}
              aria-label={panelOpen ? 'Cerrar panel de contexto' : 'Mostrar panel de contexto'}
              type="button"
              title="Panel de contexto"
            >
              <PanelToggleIcon />
            </button>
          </div>

          {/* Messages */}
          <div
            className="chat-body"
            ref={bodyRef}
            aria-live="polite"
            aria-label="Mensajes del chat"
          >
            {showWelcome ? (
              <Welcome onSuggestion={handleSuggestion} />
            ) : (
              messages.map((msg) =>
                msg.type === 'user' ? (
                  <UserMessage key={msg.id} text={msg.text} />
                ) : (
                  <AssistantMessage key={msg.id} message={msg} />
                ),
              )
            )}
          </div>

          {/* No-model CTA replaces the dead error bar */}
          {showNoModel ? (
            <NoModelBanner />
          ) : (
            <StatusBar phase={status.phase} text={statusText} />
          )}

          {/* Composer */}
          <Composer
            disabled={status.phase === 'sending'}
            isStreaming={isStreaming}
            onSend={handleSend}
            onStop={stopStream}
            value={composerText}
            onChange={setComposerText}
          />
        </div>

        {/* Context panel — mounts only when open; keeps data fresh on each open */}
        {panelOpen && (
          <ContextPanel onClose={() => setPanelOpen(false)} />
        )}
      </div>
    </>
  )
}
