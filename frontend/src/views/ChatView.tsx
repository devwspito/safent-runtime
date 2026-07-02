/**
 * ChatView — streaming chat with the Lumen agent.
 *
 * Chat state is owned by Layout and passed down via outlet context so that
 * RecentsSection (in the sidebar) and ChatView share the same instance.
 * This file is purely presentational — all state mutations go through
 * the outlet context.
 */

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ChangeEvent,
} from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { GitBranch, Loader2, CheckCircle2, AlertTriangle, FileText, X } from 'lucide-react'
import type { ChatMessage, ToolStep } from '../hooks/useChat'
import { listProviders, uploadWorkspaceFile, getRuntimeStatus, ApiError } from '../api/client'
import type { Provider } from '../api/types'
import type { ChatOutletContext } from '../components/Layout'
import ContextPanel from '../components/ContextPanel'
import PendingApprovalsInChat from '../components/PendingApprovalsInChat'
import { useT } from '../lib/i18n'
import { toolLabel } from '../lib/toolLabels'
import { useFeatures } from '../hooks/useFeatures'
import styles from './ChatView.module.css'

// ── Static strings ─────────────────────────────────────────────────────────

const STRINGS = {
  welcomeTitle:   'Hola, soy Lumen',
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

/** Map raw backend/stream errors to human-readable copy. */
function humanizeError(msg: string, t: (key: Parameters<ReturnType<typeof useT>>[0]) => string): string {
  if (/connection refused|econnrefused|network/i.test(msg)) return t('chat.err.connection')
  if (/stream_error|stream error/i.test(msg)) return t('chat.err.stream')
  if (/timeout|timed out/i.test(msg)) return t('chat.err.timeout')
  // Provider/model endpoint failures (502/503/504, bad gateway, upstream, auth):
  // point the owner at the model config instead of a vague "algo salió mal".
  if (/\b50[234]\b|bad gateway|gateway timeout|upstream|provider|unauthorized|api key|api call failed/i.test(msg))
    return t('chat.err.provider')
  return t('chat.err.generic')
}

// ── Welcome screen ─────────────────────────────────────────────────────────

interface WelcomeProps {
  onSuggestion(text: string): void
}

function Welcome({ onSuggestion }: WelcomeProps) {
  return (
    <div className={styles.welcome} role="main">
      <div className={styles.welcomeMark} aria-hidden="true">L</div>
      <h1 className={styles.welcomeTitle}>{STRINGS.welcomeTitle}</h1>
      <p className={styles.welcomeSubtitle}>{STRINGS.welcomeSubtitle}</p>
      <div className={styles.welcomeSuggestions} role="list" aria-label="Sugerencias">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            className={styles.suggestionPill}
            role="listitem"
            type="button"
            onClick={() => onSuggestion(s)}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Delegation step ────────────────────────────────────────────────────────

const DELEGATION_NAMES = new Set(['delegate_task', 'mixture_of_agents'])

/** Poll /runtime/status every 2 s while a delegation is in-flight. */
function useDelegationActivity(isActive: boolean): { tool: string; agentId: string } | null {
  const [activity, setActivity] = useState<{ tool: string; agentId: string } | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!isActive) {
      setActivity(null)
      return
    }

    let alive = true
    const poll = async () => {
      try {
        const status = await getRuntimeStatus()
        if (!alive) return
        const entry = (status.activity ?? []).find(
          (a) => a.tool && a.tool !== 'chat_responding' && a.tool !== 'delegate_task',
        )
        if (entry) {
          setActivity({ tool: entry.tool ?? 'trabajando', agentId: entry.agent_id })
        } else {
          const fallback = (status.activity ?? [])[0]
          setActivity(fallback ? { tool: fallback.tool ?? 'trabajando', agentId: fallback.agent_id } : null)
        }
      } catch {
        // Transient error — keep last state
      }
    }

    void poll()
    intervalRef.current = setInterval(() => { void poll() }, 2_000)

    return () => {
      alive = false
      if (intervalRef.current !== null) clearInterval(intervalRef.current)
    }
  }, [isActive])

  return activity
}

interface DelegationStepProps {
  step: ToolStep
  isStreaming: boolean
}

function DelegationStep({ step, isStreaming }: DelegationStepProps) {
  const specialist = step.target || step.label || 'especialista'
  const liveActivity = useDelegationActivity(isStreaming)

  return (
    <div
      className={[
        styles.delegationCard,
        isStreaming ? styles.delegationCardActive : styles.delegationCardDone,
      ].join(' ')}
      role="status"
      aria-label={isStreaming ? `Delegando a ${specialist}` : `Completado por ${specialist}`}
    >
      <span className={styles.delegationIcon} aria-hidden="true">
        <GitBranch size={14} />
      </span>
      <div className={styles.delegationBody}>
        <div className={styles.delegationLabel}>
          {isStreaming ? 'Delegando a' : 'Delegado a'}{' '}
          <span className={styles.delegationSpecialist}>{specialist}</span>
          {isStreaming ? (
            <Loader2 size={11} className="spin" aria-hidden="true" />
          ) : (
            <CheckCircle2
              size={11}
              style={{ color: 'var(--color-success)', flexShrink: 0 }}
              aria-hidden="true"
            />
          )}
        </div>
        {isStreaming && liveActivity && toolLabel(liveActivity.tool) !== null && (
          <div className={styles.delegationLive} aria-live="polite" aria-atomic="true">
            {toolLabel(liveActivity.tool)}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Tool summary block ─────────────────────────────────────────────────────

interface ToolSummaryProps {
  steps: ToolStep[]
  isStreaming: boolean
}

function ToolSummary({ steps, isStreaming }: ToolSummaryProps) {
  if (steps.length === 0) return null

  const delegations = steps.filter((s) => DELEGATION_NAMES.has(s.name))
  const regular = steps.filter((s) => !DELEGATION_NAMES.has(s.name))
  const count = regular.length
  const last = steps[steps.length - 1]

  const streamLabel = isStreaming
    ? `${last.label}${last.target ? ` — ${last.target.slice(0, 48)}` : ''}`
    : null

  return (
    <>
      {delegations.map((step, i) => (
        <DelegationStep
          key={i}
          step={step}
          isStreaming={isStreaming && i === delegations.length - 1}
        />
      ))}

      {(regular.length > 0 || (isStreaming && !DELEGATION_NAMES.has(last.name))) && (
        <details className={styles.toolGroup}>
          <summary>
            <span className={styles.toolGroupLabel}>
              {streamLabel ?? `Usó ${count} herramienta${count !== 1 ? 's' : ''}`}
            </span>
            {!isStreaming && count > 0 && (
              <span
                className={styles.toolGroupCount}
                aria-label={`${count} herramientas`}
              >
                {count}
              </span>
            )}
            <span className={styles.toolGroupChevron} aria-hidden="true">
              <ChevronIcon />
            </span>
          </summary>
          <div className={styles.toolGroupBody}>
            {regular.map((step, i) => (
              <div key={i} className={styles.toolStepItem}>
                <span className={styles.toolStepLabel}>{step.label}</span>
                {step.target && (
                  <span className={styles.toolStepTarget}>{step.target}</span>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
    </>
  )
}

// ── Thinking block ─────────────────────────────────────────────────────────

interface ThinkingBlockProps {
  text: string
  done: boolean
}

function ThinkingBlock({ text, done }: ThinkingBlockProps) {
  if (!text) return null
  return (
    <details className={styles.thinkingBlock}>
      <summary>
        <span className={styles.thinkingLabel}>
          {done ? 'Proceso de pensamiento' : 'Pensando…'}
        </span>
        <span className={styles.thinkingChevron} aria-hidden="true">
          <ChevronIcon />
        </span>
      </summary>
      <div className={styles.thinkingBody}>{text}</div>
    </details>
  )
}

// ── Message bubbles ────────────────────────────────────────────────────────

interface UserMessageProps {
  text: string
  failed?: boolean
  enterDelay?: number
}

const UserMessage = memo(function UserMessage({ text, failed, enterDelay = 0 }: UserMessageProps) {
  const t = useT()
  return (
    <div
      className={[styles.messageRow, styles.messageRowUser].join(' ')}
      style={{ animationDelay: `${enterDelay}ms` }}
      role="article"
      aria-label="Tu mensaje"
    >
      <div className={[styles.userBubble, failed ? styles.userBubbleFailed : ''].join(' ')}>
        {text}
      </div>
      {failed && (
        <p className={styles.userFailedNote} role="alert">
          {t('chat.err.not_sent')}
        </p>
      )}
    </div>
  )
})

// The SPA's own view routes (react-router basename=/app). When the agent guides
// the user with a markdown link to one of these (e.g. [Abrir Archivos](/archivos)),
// we intercept the click and navigate IN-APP instead of doing a full-page reload
// that would 404 (the app is mounted under /app). This is how the agent "operates
// its own body" — it can take the user to any section with a one-click button.
const APP_VIEW_ROUTES: ReadonlySet<string> = new Set([
  '/chat', '/programadas', '/agentes', '/skills', '/integraciones', '/mcp',
  '/archivos', '/proveedores', '/seguridad', '/memoria', '/coste', '/en-vivo', '/ensenar',
])

interface AssistantMessageProps {
  message: Extract<ChatMessage, { type: 'assistant' }>
  enterDelay?: number
}

const AssistantMessage = memo(function AssistantMessage({
  message,
  enterDelay = 0,
}: AssistantMessageProps) {
  const { thinkingText, thinkingDone, toolSteps, activityText, renderedHtml, isStreaming } = message
  const navigate = useNavigate()

  // Intercept clicks on internal view links inside the agent's rendered markdown
  // and route them through react-router (respects the /app basename); external
  // links (target=_blank) are left untouched.
  const handleProseClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const anchor = (e.target as HTMLElement).closest('a')
    if (!anchor) return
    const href = anchor.getAttribute('href') ?? ''
    if (!href.startsWith('/')) return
    const path = href.split('?')[0].split('#')[0]
    if (APP_VIEW_ROUTES.has(path)) {
      e.preventDefault()
      navigate(path)
    }
  }, [navigate])

  const hasDelegationInFlight =
    isStreaming &&
    toolSteps.length > 0 &&
    DELEGATION_NAMES.has(toolSteps[toolSteps.length - 1]!.name)

  return (
    <div
      className={styles.messageRow}
      style={{ animationDelay: `${enterDelay}ms` }}
      role="article"
      aria-label="Respuesta de Lumen"
    >
      <div className={styles.agentOutput}>
        <ThinkingBlock text={thinkingText} done={thinkingDone} />
        <ToolSummary steps={toolSteps} isStreaming={isStreaming} />

        {/* Live activity excerpt while streaming */}
        {isStreaming && activityText && (
          <div className={styles.agentActivity} aria-live="polite" aria-atomic="false">
            {lastLine(activityText)}
          </div>
        )}

        {/* Final rendered markdown */}
        {!isStreaming && renderedHtml && (
          <div
            className={styles.agentProse}
            onClick={handleProseClick}
            /* Safe: renderedHtml is produced by DOMPurify.sanitize — see lib/markdown.ts */
            dangerouslySetInnerHTML={{ __html: renderedHtml }}
          />
        )}

        {/* Streaming cursor — hidden when a delegation card already shows a live spinner */}
        {isStreaming && !activityText && !hasDelegationInFlight && (
          <div className={styles.agentActivity} aria-live="polite">
            <span className={styles.streamCursor} aria-hidden="true" />
          </div>
        )}
      </div>
    </div>
  )
})

function lastLine(text: string): string {
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean)
  return lines.length ? `· ${lines[lines.length - 1]}` : ''
}

// ── Status bar ─────────────────────────────────────────────────────────────

interface StatusBarProps {
  phase: string
  text?: string
}

function StatusBar({ phase, text }: StatusBarProps) {
  if (phase === 'idle') return null
  const isError = phase === 'error'

  return (
    <div
      className={[styles.statusBar, isError ? styles.statusBarError : ''].join(' ')}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
    >
      {!isError && <SpinnerIcon />}
      <span>{text}</span>
    </div>
  )
}

// ── No-model CTA banner ────────────────────────────────────────────────────

function NoModelBanner() {
  const navigate = useNavigate()
  const t = useT()
  return (
    <div className={styles.noModelBanner} role="alert">
      <span>{t('chat.nomodel.text')}</span>
      <button
        type="button"
        className="cv-btn cv-btn--primary cv-btn--sm"
        onClick={() => navigate('/proveedores')}
      >
        {t('chat.nomodel.cta')}
      </button>
    </div>
  )
}

// ── Model picker ───────────────────────────────────────────────────────────

function useActiveProvider() {
  const [provider, setProvider] = useState<Provider | null>(null)

  useEffect(() => {
    listProviders()
      .then((data) => {
        const arr = Array.isArray(data) ? data : []
        setProvider(arr.find((p) => p.is_active) ?? arr[0] ?? null)
      })
      .catch(() => setProvider(null))
  }, [])

  return provider
}

function ModelPicker() {
  const navigate = useNavigate()
  const provider = useActiveProvider()
  const { allowed } = useFeatures()

  // Cloud-managed associate: the `proveedores` view is gated, so listProviders()
  // 403s and `provider` stays null — but a model IS resolved server-side from the
  // org's policy. Showing "Sin modelo" (and linking to a blocked view) is wrong:
  // surface that the model is org-managed and make the chip inert instead.
  const orgManaged = !allowed('proveedores')

  if (orgManaged) {
    return (
      <span
        className={styles.modelPicker}
        title="El modelo lo gestiona tu organización"
        aria-label="Modelo gestionado por tu organización"
      >
        <span className={styles.modelPickerLabel}>Modelo gestionado</span>
      </span>
    )
  }

  const label = provider
    ? (provider.default_model ?? provider.alias ?? provider.name ?? 'Modelo activo')
    : 'Sin modelo'

  return (
    <button
      className={styles.modelPicker}
      onClick={() => navigate('/proveedores')}
      title={
        provider
          ? `Proveedor: ${provider.alias ?? provider.name}`
          : 'Configura un modelo en Proveedores'
      }
      type="button"
      aria-label={
        provider
          ? `Modelo activo: ${label}. Ir a Proveedores`
          : 'Sin modelo. Ir a Proveedores'
      }
    >
      <span className={styles.modelPickerLabel}>{label}</span>
      <ChevronIcon />
    </button>
  )
}

// ── Attachment chip ────────────────────────────────────────────────────────

interface AttachmentChipProps {
  name: string
  uploading: boolean
  error: boolean
  onRemove: () => void
}

function AttachmentChip({ name, uploading, error, onRemove }: AttachmentChipProps) {
  return (
    <div
      className={[styles.attachChip, error ? styles.attachChipError : ''].join(' ')}
      title={name}
    >
      {uploading ? (
        <Loader2 size={12} className="spin" aria-hidden="true" />
      ) : error ? (
        <AlertTriangle size={12} aria-hidden="true" />
      ) : (
        <FileText size={12} aria-hidden="true" />
      )}
      <span className={styles.attachChipName}>{name}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Quitar adjunto ${name}`}
        className={styles.attachChipRemove}
        disabled={uploading}
      >
        <X size={12} aria-hidden="true" />
      </button>
    </div>
  )
}

// ── Composer ───────────────────────────────────────────────────────────────

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
  const t = useT()
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
          const msg =
            err instanceof ApiError
              ? err.message
              : t('chat.err.attach').replace('{name}', att.file.name)
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
  const canSend =
    !disabled && !anyUploading && (value.trim() !== '' || attachments.some((a) => a.uploadedPath))

  return (
    <div className={styles.composerWrap}>
      {attachments.length > 0 && (
        <div className={styles.attachmentsRow} aria-label="Archivos adjuntos">
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

      <div className={styles.composerBox}>
        <textarea
          ref={textareaRef}
          className={styles.composerTextarea}
          placeholder={STRINGS.placeholder}
          aria-label="Escribe un mensaje para Lumen"
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
        />
        <div className={styles.composerToolbar}>
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
            className={styles.attachBtn}
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            aria-label="Adjuntar archivo (imágenes, PDF, documentos)"
            title="Adjuntar archivo"
          >
            <AttachIcon />
          </button>

          <ModelPicker />

          <div className={styles.composerToolbarRight}>
            {isStreaming ? (
              <button
                type="button"
                className={styles.stopBtn}
                onClick={onStop}
                aria-label="Detener generación"
              >
                {STRINGS.stop}
              </button>
            ) : (
              <button
                type="button"
                className={styles.sendBtn}
                onClick={handleSend}
                disabled={!canSend}
                aria-label="Enviar mensaje (Enter)"
                aria-busy={anyUploading}
              >
                {anyUploading ? 'Subiendo…' : STRINGS.send}
              </button>
            )}
          </div>
        </div>
      </div>
      <p className={styles.composerFooter}>{STRINGS.disclaimer}</p>
    </div>
  )
}

// ── Micro icons ────────────────────────────────────────────────────────────

function ChevronIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <path
        d="M4 3l4 3-4 3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function AttachIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
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
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M10 2v12" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

function SpinnerIcon() {
  return (
    <svg
      className="spin"
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      aria-hidden="true"
    >
      <circle
        cx="6"
        cy="6"
        r="4.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeDasharray="14 8"
      />
    </svg>
  )
}

// ── ChatView ───────────────────────────────────────────────────────────────

export default function ChatView() {
  const t = useT()
  const { convId, agentName, messages, status, sendMessage, stopStream, approvalRefreshTick } =
    useOutletContext<ChatOutletContext>()
  const [composerText, setComposerText] = useState('')
  const [panelOpen, setPanelOpen] = useState(true)
  const [showNoModel, setShowNoModel] = useState(false)
  const [noProvider, setNoProvider] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const userScrolledRef = useRef(false)
  const pinRef = useRef(true)

  // Proactively surface "connect a model" alert
  useEffect(() => {
    let alive = true
    listProviders()
      .then((data) => {
        const arr = Array.isArray(data) ? data : []
        if (alive) setNoProvider(arr.length === 0)
      })
      .catch(() => {
        /* transient — 409 path still covers it */
      })
    return () => {
      alive = false
    }
  }, [])

  const isStreaming = status.phase === 'streaming' || status.phase === 'sending'
  const showWelcome = messages.length === 0

  // Detect no-model 409
  useEffect(() => {
    if (status.phase === 'error') {
      const msg = (status as { phase: 'error'; message: string }).message ?? ''
      const isNoModel =
        msg.includes('409') || /sin modelo|no model|no provider|no.*provider/i.test(msg)
      setShowNoModel(isNoModel)
    } else {
      setShowNoModel(false)
    }
  }, [status])

  // Scroll pinning
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

  const handleSend = useCallback(
    (text: string) => {
      userScrolledRef.current = false
      pinRef.current = true
      setComposerText('')
      setShowNoModel(false)
      void sendMessage(text)
    },
    [sendMessage],
  )

  const handleSuggestion = useCallback(
    (text: string) => {
      handleSend(text)
    },
    [handleSend],
  )

  const statusText =
    status.phase === 'streaming'
      ? status.statusText
      : status.phase === 'sending'
        ? 'Enviando…'
        : status.phase === 'error' && !showNoModel
          ? humanizeError(
              (status as { phase: 'error'; message: string }).message ?? '',
              t,
            )
          : undefined

  return (
    <>
      <div className={styles.chatShell}>
        <div className={styles.chatView}>
          {/* Topbar */}
          <div className={styles.topbar}>
            <span className={styles.topbarTitle}>
              {agentName
                ? `Hablando con ${agentName}`
                : showWelcome
                  ? 'Nueva conversación'
                  : 'Chat'}
            </span>
            <button
              className={styles.topbarPanelBtn}
              onClick={() => setPanelOpen((v) => !v)}
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
            className={styles.chatBody}
            ref={bodyRef}
            aria-live="polite"
            aria-label="Mensajes del chat"
          >
            {showWelcome ? (
              <Welcome onSuggestion={handleSuggestion} />
            ) : (
              messages.map((msg, idx) =>
                msg.type === 'user' ? (
                  <UserMessage
                    key={msg.id}
                    text={msg.text}
                    enterDelay={Math.min(idx * 30, 180)}
                  />
                ) : (
                  <AssistantMessage
                    key={msg.id}
                    message={msg}
                    enterDelay={Math.min(idx * 30, 180)}
                  />
                ),
              )
            )}
            {/* Approval cards inline */}
            <PendingApprovalsInChat
              currentThreadId={convId}
              refreshTick={approvalRefreshTick}
            />
          </div>

          {showNoModel || noProvider ? (
            <NoModelBanner />
          ) : (
            <StatusBar phase={status.phase} text={statusText} />
          )}

          <Composer
            disabled={status.phase === 'sending' || status.phase === 'streaming'}
            isStreaming={isStreaming}
            onSend={handleSend}
            onStop={stopStream}
            value={composerText}
            onChange={setComposerText}
          />
        </div>

        {panelOpen && <ContextPanel onClose={() => setPanelOpen(false)} />}
      </div>
    </>
  )
}
