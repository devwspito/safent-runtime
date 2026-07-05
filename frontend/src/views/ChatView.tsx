/**
 * ChatView — streaming chat with the Safent agent.
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
import { GitBranch, Loader2, CheckCircle2, AlertTriangle, FileText, X, Plus, Paperclip, FolderOpen, Zap, Check, Maximize2, ChevronDown, ChevronRight, ChevronLeft } from 'lucide-react'
import { VncFrame } from '../components/VncView'
import type { ChatMessage, ToolStep } from '../hooks/useChat'
import { listProviders, uploadWorkspaceFile, getRuntimeStatus, listSkills, ApiError } from '../api/client'
import type { Provider, Skill } from '../api/types'
import {
  uploadDirectoryToBridge,
  syncBridgeToHost,
  pickHostDirectory,
  supportsFolderPicker,
  type BridgeSelection,
} from '../lib/folderBridge'
import { sileo } from 'sileo'
import type { ChatOutletContext } from '../components/Layout'
import ContextPanel from '../components/ContextPanel'
import PendingApprovalsInChat from '../components/PendingApprovalsInChat'
import { useT } from '../lib/i18n'
import { toolLabel } from '../lib/toolLabels'
import { isLiveSkill } from '../lib/skills'
import { useFeatures } from '../hooks/useFeatures'
import styles from './ChatView.module.css'

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
  const t = useT()
  const suggestions = [
    t('chat.suggest.1'),
    t('chat.suggest.2'),
    t('chat.suggest.3'),
    t('chat.suggest.4'),
  ]
  return (
    <div className={styles.welcome} role="main">
      <div className={styles.welcomeMark} aria-hidden="true">L</div>
      <h1 className={styles.welcomeTitle}>{t('chat.welcome.title')}</h1>
      <p className={styles.welcomeSubtitle}>{t('chat.welcome.subtitle')}</p>
      <div className={styles.welcomeSuggestions} role="list" aria-label={t('chat.suggestions_aria')}>
        {suggestions.map((s) => (
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
  const t = useT()
  const workingFallback = t('chat.delegation.working_fallback')

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
          setActivity({ tool: entry.tool ?? workingFallback, agentId: entry.agent_id })
        } else {
          const fallback = (status.activity ?? [])[0]
          setActivity(fallback ? { tool: fallback.tool ?? workingFallback, agentId: fallback.agent_id } : null)
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
  }, [isActive, workingFallback])

  return activity
}

interface DelegationStepProps {
  step: ToolStep
  isStreaming: boolean
}

function DelegationStep({ step, isStreaming }: DelegationStepProps) {
  const t = useT()
  const specialist = step.target || step.label || t('chat.delegation.specialist_fallback')
  const liveActivity = useDelegationActivity(isStreaming)

  return (
    <div
      className={[
        styles.delegationCard,
        isStreaming ? styles.delegationCardActive : styles.delegationCardDone,
      ].join(' ')}
      role="status"
      aria-label={
        isStreaming
          ? t('chat.delegation.aria_active').replace('{specialist}', specialist)
          : t('chat.delegation.aria_done').replace('{specialist}', specialist)
      }
    >
      <span className={styles.delegationIcon} aria-hidden="true">
        <GitBranch size={14} />
      </span>
      <div className={styles.delegationBody}>
        <div className={styles.delegationLabel}>
          {isStreaming ? t('chat.delegation.delegating_to') : t('chat.delegation.delegated_to')}{' '}
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
  const t = useT()
  if (steps.length === 0) return null

  const delegations = steps.filter((s) => DELEGATION_NAMES.has(s.name))
  const regular = steps.filter((s) => !DELEGATION_NAMES.has(s.name))
  const count = regular.length
  const last = steps[steps.length - 1]

  const streamLabel = isStreaming
    ? `${last.label}${last.target ? ` — ${last.target.slice(0, 48)}` : ''}`
    : null
  const toolsUsedLabel = t(count === 1 ? 'chat.tools.used' : 'chat.tools.used_pl').replace(
    '{count}',
    String(count),
  )

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
              {streamLabel ?? toolsUsedLabel}
            </span>
            {!isStreaming && count > 0 && (
              <span
                className={styles.toolGroupCount}
                aria-label={t('chat.tools.count_aria').replace('{count}', String(count))}
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
  const t = useT()
  if (!text) return null
  return (
    <details className={styles.thinkingBlock}>
      <summary>
        <span className={styles.thinkingLabel}>
          {done ? t('chat.thinking.done') : t('chat.thinking')}
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
      aria-label={t('chat.aria.message')}
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
  const t = useT()

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

  // Auto-scroll the live activity block to its tail as new text streams in —
  // without this the growing answer scrolls out of view above the fold and
  // the user only ever sees whatever happened to be at the bottom on the last
  // render (see ChatView.module.css .agentActivity for the scrollable box).
  const activityRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (isStreaming && activityRef.current) {
      activityRef.current.scrollTop = activityRef.current.scrollHeight
    }
  }, [activityText, isStreaming])

  return (
    <div
      className={styles.messageRow}
      style={{ animationDelay: `${enterDelay}ms` }}
      role="article"
      aria-label={t('chat.aria.reply')}
    >
      <div className={styles.agentOutput}>
        <ThinkingBlock text={thinkingText} done={thinkingDone} />
        <ToolSummary steps={toolSteps} isStreaming={isStreaming} />

        {/* Live activity — the FULL text streamed so far for this segment (not
            just the last line), auto-scrolled to the tail as it grows. Showing
            only the last line hid every paragraph/step before it, making a
            fluid multi-paragraph answer look like it "freezes" until done. */}
        {isStreaming && activityText && (
          <div ref={activityRef} className={styles.agentActivity} aria-live="polite" aria-atomic="false">
            {activityText}
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
  const t = useT()

  // Cloud-managed associate: the `proveedores` view is gated, so listProviders()
  // 403s and `provider` stays null — but a model IS resolved server-side from the
  // org's policy. Showing "Sin modelo" (and linking to a blocked view) is wrong:
  // surface that the model is org-managed and make the chip inert instead.
  const orgManaged = !allowed('proveedores')

  if (orgManaged) {
    return (
      <span
        className={styles.modelPicker}
        title={t('chat.model.org_managed_title')}
        aria-label={t('chat.model.org_managed_aria')}
      >
        <span className={styles.modelPickerLabel}>{t('chat.model.org_managed_label')}</span>
      </span>
    )
  }

  const label = provider
    ? (provider.default_model ?? provider.alias ?? provider.name ?? t('chat.model.active_fallback'))
    : t('chat.model.none')

  return (
    <button
      className={styles.modelPicker}
      onClick={() => navigate('/proveedores')}
      title={
        provider
          ? t('chat.model.provider_title').replace('{provider}', String(provider.alias ?? provider.name))
          : t('chat.model.configure_title')
      }
      type="button"
      aria-label={
        provider
          ? t('chat.model.active_aria').replace('{name}', label)
          : t('chat.model.none_aria')
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
  const t = useT()
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
        aria-label={t('chat.attach.remove_aria').replace('{name}', name)}
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

  // "+" context menu: two-level (root → skills submenu). Non-exclusive.
  const [menuOpen, setMenuOpen] = useState(false)
  const [menuView, setMenuView] = useState<'root' | 'skills'>('root')
  const [skills, setSkills] = useState<Skill[]>([])
  const [skillsLoaded, setSkillsLoaded] = useState(false)
  const [selectedSkills, setSelectedSkills] = useState<Skill[]>([])
  const [bridge, setBridge] = useState<BridgeSelection | null>(null)
  const [bridgeBusy, setBridgeBusy] = useState(false)
  const [bridgeSyncing, setBridgeSyncing] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const plusBtnRef = useRef<HTMLButtonElement>(null)

  const skillKey = (sk: Skill) => sk.skill_id ?? sk.package_id ?? sk.skill_name ?? sk.name ?? ''
  const skillLabel = (sk: Skill) => sk.skill_name ?? sk.name ?? sk.slug ?? skillKey(sk)
  const isLive = isLiveSkill

  function openMenu() {
    setMenuView('root')
    setMenuOpen((o) => !o)
  }

  // Load skills lazily — only when the user actually opens the Skills submenu.
  async function enterSkillsView() {
    setMenuView('skills')
    if (!skillsLoaded) {
      try {
        const list = await listSkills()
        setSkills(Array.isArray(list) ? list : [])
      } catch { /* fail-soft: empty picker */ }
      setSkillsLoaded(true)
    }
  }

  function toggleSkill(sk: Skill) {
    const k = skillKey(sk)
    setSelectedSkills((prev) =>
      prev.some((s) => skillKey(s) === k) ? prev.filter((s) => skillKey(s) !== k) : [...prev, sk],
    )
  }

  async function pickFolder() {
    if (!supportsFolderPicker()) {
      sileo.error({ title: t('chat.folder.picker_unsupported') })
      return
    }
    setMenuOpen(false)
    let handle: FileSystemDirectoryHandle | null
    try {
      handle = await pickHostDirectory()
    } catch (e) {
      sileo.error({ title: e instanceof Error ? e.message : t('chat.folder.picker_err') })
      return
    }
    if (!handle) return
    setBridgeBusy(true)
    try {
      const sel = await uploadDirectoryToBridge(handle)
      setBridge(sel)
      sileo.success({
        title: t('chat.folder.ready').replace('{name}', sel.name).replace('{count}', String(sel.fileCount)),
      })
    } catch (e) {
      sileo.error({ title: e instanceof Error ? e.message : t('chat.folder.load_err') })
    } finally {
      setBridgeBusy(false)
    }
  }

  async function syncBridge() {
    if (!bridge) return
    setBridgeSyncing(true)
    try {
      const n = await syncBridgeToHost(bridge)
      sileo.success({ title: t('chat.folder.saved').replace('{count}', String(n)) })
    } catch (e) {
      sileo.error({ title: e instanceof Error ? e.message : t('chat.folder.save_err') })
    } finally {
      setBridgeSyncing(false)
    }
  }

  // Close the menu on outside click (ignore clicks on the popover or the "+" button).
  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (e: MouseEvent) => {
      const tgt = e.target as Node
      if (menuRef.current?.contains(tgt) || plusBtnRef.current?.contains(tgt)) return
      setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuOpen])

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

    // Compose context refs (same channel as attachments: appended as text the
    // agent reads). Non-exclusive: skills + folder + attachments can all be present.
    const refs: string[] = []
    if (selectedSkills.length > 0) {
      refs.push(`Usa estas habilidades: ${selectedSkills.map(skillLabel).join(', ')}.`)
    }
    if (bridge) {
      refs.push(
        `Trabaja en la carpeta ${bridge.workspacePath} (es la carpeta "${bridge.name}" del usuario, puedes leer y modificar sus ficheros).`,
      )
    }
    if (uploadedPaths.length > 0) {
      refs.push(uploadedPaths.map((p) => `[Adjunto: ${p}]`).join('\n'))
    }

    let text = value
    if (refs.length > 0) {
      const block = refs.join('\n')
      text = text.trim() ? `${text}\n\n${block}` : block
    }

    if (text.trim()) {
      onSend(text)
      setAttachments([])
      setSelectedSkills([])
      // Keep `bridge` so the user can still "Guardar en mi carpeta" after the
      // agent finishes; they remove it explicitly with the chip's ✕.
    }
  }

  const anyUploading = attachments.some((a) => a.uploading)
  const hasContext =
    attachments.some((a) => a.uploadedPath) || selectedSkills.length > 0 || bridge !== null
  const canSend = !disabled && !anyUploading && !bridgeBusy && (value.trim() !== '' || hasContext)

  return (
    <div className={styles.composerWrap}>
      {(attachments.length > 0 || selectedSkills.length > 0 || bridge || bridgeBusy) && (
        <div className={styles.attachmentsRow} aria-label={t('chat.context_aria')}>
          {attachments.map((att) => (
            <AttachmentChip
              key={att.id}
              name={att.file.name}
              uploading={att.uploading}
              error={att.error}
              onRemove={() => removeAttachment(att.id)}
            />
          ))}
          {selectedSkills.map((sk) => (
            <span key={skillKey(sk)} className={styles.attachChip}>
              <Zap size={12} aria-hidden="true" />
              {skillLabel(sk)}{isLive(sk) ? ` · ${t('skills.live.badge')}` : ''}
              <button
                type="button"
                onClick={() => toggleSkill(sk)}
                aria-label={t('chat.skill.remove_aria').replace('{name}', skillLabel(sk))}
                className={styles.attachChipRemove}
              >
                <X size={11} aria-hidden="true" />
              </button>
            </span>
          ))}
          {bridgeBusy && (
            <span className={styles.attachChip}>
              <Loader2 size={12} className="spin" aria-hidden="true" /> {t('chat.folder.loading')}
            </span>
          )}
          {bridge && (
            <span className={styles.attachChip} title={bridge.workspacePath}>
              <FolderOpen size={12} aria-hidden="true" />
              {bridge.name} ({bridge.fileCount})
              <button
                type="button"
                onClick={() => void syncBridge()}
                disabled={bridgeSyncing}
                aria-label={t('chat.folder.save_aria')}
                title={t('chat.folder.save_title')}
                className={styles.attachChipRemove}
              >
                {bridgeSyncing ? <Loader2 size={11} className="spin" /> : <Check size={11} />}
              </button>
              <button
                type="button"
                onClick={() => setBridge(null)}
                aria-label={t('chat.folder.remove_aria')}
                className={styles.attachChipRemove}
              >
                <X size={11} aria-hidden="true" />
              </button>
            </span>
          )}
        </div>
      )}

      <div className={styles.composerBox}>
        {menuOpen && (
          <div ref={menuRef} className={styles.plusMenu} role="menu" aria-label={t('chat.menu.aria')}>
            {menuView === 'root' && (
              <>
                <button type="button" className={styles.plusItem} role="menuitem"
                  onClick={() => { setMenuOpen(false); fileInputRef.current?.click() }}>
                  <Paperclip size={14} aria-hidden="true" /> {t('chat.menu.attach')}
                </button>
                <button type="button" className={styles.plusItem} role="menuitem" onClick={() => void pickFolder()}>
                  <FolderOpen size={14} aria-hidden="true" /> {t('chat.menu.folder')}
                </button>
                <button type="button" className={styles.plusItem} role="menuitem" onClick={() => void enterSkillsView()}>
                  <Zap size={14} aria-hidden="true" />
                  <span className={styles.plusItemLabel}>{t('nav.skills')}</span>
                  <ChevronRight size={14} aria-hidden="true" />
                </button>
              </>
            )}

            {menuView === 'skills' && (
              <>
                <button type="button" className={styles.plusItem} role="menuitem"
                  onClick={() => setMenuView('root')}>
                  <ChevronLeft size={14} aria-hidden="true" />
                  <span className={styles.plusItemLabel}>{t('nav.skills')}</span>
                </button>
                {(() => {
                  if (!skillsLoaded) return <div className={styles.plusEmpty}>{t('chat.menu.loading')}</div>
                  if (skills.length === 0) return <div className={styles.plusEmpty}>{t('chat.menu.none')}</div>
                  return skills.map((sk) => {
                    const on = selectedSkills.some((s) => skillKey(s) === skillKey(sk))
                    return (
                      <button key={skillKey(sk)} type="button" className={styles.plusItem} role="menuitemcheckbox"
                        aria-checked={on} onClick={() => toggleSkill(sk)}>
                        <Zap size={14} aria-hidden="true" />
                        <span className={[styles.plusItemLabel, styles.plusItemLabelEllipsis].join(' ')}>{skillLabel(sk)}</span>
                        {isLive(sk) && <span className={styles.plusLiveTag}>{t('skills.live.badge')}</span>}
                        {on && <Check size={13} aria-hidden="true" />}
                      </button>
                    )
                  })
                })()}
              </>
            )}
          </div>
        )}
        <textarea
          ref={textareaRef}
          className={styles.composerTextarea}
          placeholder={t('chat.placeholder')}
          aria-label={t('chat.aria.textarea')}
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
            aria-label={t('chat.attach.input_aria')}
            onChange={handleFileSelect}
            tabIndex={-1}
          />
          <button
            ref={plusBtnRef}
            type="button"
            className={styles.attachBtn}
            onClick={openMenu}
            disabled={disabled}
            aria-label={t('chat.aria.add_context')}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title={t('chat.context.add_title')}
          >
            <Plus size={16} />
          </button>

          <ModelPicker />

          <div className={styles.composerToolbarRight}>
            {isStreaming ? (
              <button
                type="button"
                className={styles.stopBtn}
                onClick={onStop}
                aria-label={t('chat.aria.stop')}
              >
                {t('chat.stop')}
              </button>
            ) : (
              <button
                type="button"
                className={styles.sendBtn}
                onClick={handleSend}
                disabled={!canSend}
                aria-label={t('chat.aria.send')}
                aria-busy={anyUploading}
              >
                {anyUploading ? t('chat.uploading') : t('chat.send')}
              </button>
            )}
          </div>
        </div>
      </div>
      <p className={styles.composerFooter}>{t('chat.disclaimer')}</p>
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

/**
 * LiveBrowserPanel — inline "Ver en vivo" for the chat. Shown while the in-flight
 * turn's task is using the browser (liveBrowserActive). Collapsed by default: a red
 * "Ver en vivo" chip that expands a view-only VNC frame of the jailed browser, with a
 * fullscreen toggle. Same VNC system as Enseñar / En vivo (sharp + fluid).
 */
function LiveBrowserPanel() {
  const t = useT()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  const toggleFullscreen = () => {
    const el = wrapRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {})
    else void el.requestFullscreen?.().catch(() => {})
  }

  return (
    <div className={styles.liveBrowserWrap}>
      {/* Prominent live CARD (owner ask): says what's happening and jumps to the
          En vivo tab on click; the chevron side still expands the inline frame. */}
      <div className={styles.liveCard}>
        <span className={styles.liveBrowserDot} aria-hidden="true" />
        <span className={styles.liveCardText}>{t('chat.live.card')}</span>
        <button
          type="button"
          className={styles.liveCardGo}
          onClick={() => navigate('/capacidades?tab=en-vivo')}
        >
          {t('chat.live.open')} →
        </button>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label={t('chat.live.watch')}
          title={t('chat.live.watch')}
          className={styles.liveBrowserChip}
        >
          <ChevronDown
            size={14}
            aria-hidden="true"
            className={[styles.liveBrowserChevron, open ? styles.liveBrowserChevronOpen : ''].join(' ')}
          />
        </button>
      </div>
      {open && (
        <div ref={wrapRef} className={styles.liveBrowserFrame}>
          <button
            type="button"
            onClick={toggleFullscreen}
            aria-label={t('agents.fullscreen')}
            title={t('agents.fullscreen')}
            className={styles.liveBrowserFullscreenBtn}
          >
            <Maximize2 size={14} aria-hidden="true" />
          </button>
          <VncFrame viewOnly />
        </div>
      )}
    </div>
  )
}

export default function ChatView() {
  const t = useT()
  const { convId, agentName, messages, status, sendMessage, stopStream, approvalRefreshTick, liveBrowserActive } =
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
        ? t('chat.sending')
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
                ? t('chat.topbar.talking_to').replace('{name}', agentName)
                : showWelcome
                  ? t('chat.topbar.new_conversation')
                  : t('nav.chat')}
            </span>
            <button
              className={styles.topbarPanelBtn}
              onClick={() => setPanelOpen((v) => !v)}
              aria-pressed={panelOpen}
              aria-label={panelOpen ? t('ctx.panel.close.aria') : t('chat.panel.show_aria')}
              type="button"
              title={t('chat.panel.title')}
            >
              <PanelToggleIcon />
            </button>
          </div>

          {/* Messages */}
          <div
            className={styles.chatBody}
            ref={bodyRef}
            aria-live="polite"
            aria-label={t('chat.aria.messages')}
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

          {liveBrowserActive && <LiveBrowserPanel />}

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

        {panelOpen && <ContextPanel onClose={() => setPanelOpen(false)} busy={status.phase === 'streaming'} />}
      </div>
    </>
  )
}
