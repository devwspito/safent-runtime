/**
 * TeachingView — enseña al agente una habilidad demostrando en un navegador LIVE.
 *
 * WEBSOCKET CONTRACT (implementado aquí; el backend debe coincidir en /api/v1/training/{id}/live):
 *
 *   URL:     ws(s)://<same-origin>/api/v1/training/{sessionId}/live?token=<operatorToken>
 *            Protocolo ws: cuando la página es http:, wss: cuando es https:.
 *
 *   INBOUND (servidor → cliente):
 *     Mensajes BINARIOS (Blob / ArrayBuffer) = bytes JPEG del frame actual.
 *     El cliente renderiza cada frame en un <canvas> con object-fit: contain.
 *     El canvas registra la escala + desplazamiento de letterbox para mapear
 *     coords de canvas → coords del navegador remoto (aprox. 1280×720 px).
 *
 *   OUTBOUND (cliente → servidor, texto JSON):
 *     Ratón:    { "type": "mouse",    "action": "down"|"up"|"move",
 *                 "x": <int>, "y": <int>, "button": 0|1|2 }
 *               x, y = coords en el espacio del navegador remoto (NO del canvas).
 *               mousemove se throttlea a ~50 ms para no saturar el canal.
 *     Teclado:  { "type": "key",      "action": "down"|"up"|"char",
 *                 "keysym": <int|null>, "text": <string|null> }
 *               keysym = código XKB (null si no disponible en la Web).
 *               text   = ev.key (string del evento de teclado de JavaScript).
 *     Navegar:  { "type": "navigate", "url": <string> }
 *               Abre o navega a la URL indicada en el navegador remoto.
 */

import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
} from 'react'
import { sileo } from 'sileo'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, Maximize2, Monitor, MessageSquare, X } from 'lucide-react'
import {
  createTrainingSession,
  startTrainingRecording,
  pauseTrainingRecording,
  resumeTrainingRecording,
  stopTrainingRecording,
  cancelTrainingRecording,
  signTrainingSession,
  getTrainingState,
  postChat,
  ApiError,
} from '../api/client'
import { token } from '../lib/token'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { FadeIn } from '../components/ui/motion'
import s from './TeachingView.module.css'

// ── Domain types ──────────────────────────────────────────────────────────────

type SessionPhaseState =
  | 'idle'
  | 'capturing'
  | 'paused'
  | 'review'
  | 'validated'
  | 'cancelled'
  | 'abandoned'

interface Session {
  sessionId: string
  skillName: string
  sessionState: SessionPhaseState
}

// Discriminated union makes impossible UI states impossible:
// we can't be in 'live' without a session, or in 'review' without stopping.
type ViewState =
  | { phase: 'setup' }
  | { phase: 'connecting'; session: Session }
  | { phase: 'live';       session: Session; wsReady: boolean }
  | { phase: 'review';     session: Session }
  | { phase: 'error';      message: string }

type ViewAction =
  | { type: 'START_CONNECTING'; session: Session }
  | { type: 'WS_OPEN' }
  | { type: 'REMOTE_STATE'; sessionState: SessionPhaseState }
  | { type: 'STOP' }
  | { type: 'SIGN_OK' }
  | { type: 'CANCEL' }
  | { type: 'ERROR'; message: string }

function reducer(vs: ViewState, action: ViewAction): ViewState {
  switch (action.type) {
    case 'START_CONNECTING':
      return { phase: 'connecting', session: action.session }

    case 'WS_OPEN':
      if (vs.phase !== 'connecting' && vs.phase !== 'live') return vs
      return { phase: 'live', session: vs.session, wsReady: true }

    case 'REMOTE_STATE': {
      const { sessionState } = action
      if (vs.phase !== 'live' && vs.phase !== 'connecting') return vs
      const session: Session = { ...vs.session, sessionState }
      if (sessionState === 'review' || sessionState === 'validated') {
        return { phase: 'review', session }
      }
      if (sessionState === 'cancelled' || sessionState === 'abandoned') {
        return { phase: 'setup' }
      }
      if (vs.phase === 'live') return { ...vs, session }
      return { phase: 'live', session, wsReady: false }
    }

    case 'STOP':
      if (vs.phase !== 'live') return vs
      return { phase: 'review', session: { ...vs.session, sessionState: 'review' } }

    case 'SIGN_OK':
      return { phase: 'setup' }

    case 'CANCEL':
      return { phase: 'setup' }

    case 'ERROR':
      return { phase: 'error', message: action.message }
  }
}

// ── Utility helpers ───────────────────────────────────────────────────────────

function wsUrl(sessionId: string, tok: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/api/v1/training/${encodeURIComponent(sessionId)}/live?token=${encodeURIComponent(tok)}`
}

function toastOk(msg: string) { sileo.success({ title: msg }) }
function toastWarn(msg: string) { sileo.warning({ title: msg }) }
function toastErr(msg: string) { sileo.error({ title: msg }) }

function sessionStateLabel(s: SessionPhaseState): string {
  switch (s) {
    case 'capturing': return 'Grabando'
    case 'paused':    return 'Pausado'
    case 'review':    return 'Revisión'
    case 'validated': return 'Guardado'
    default:          return s
  }
}

function pillClass(sessionState: SessionPhaseState): string {
  if (sessionState === 'capturing') return `${s.statusPill} ${s['statusPill--capturing']}`
  if (sessionState === 'paused')    return `${s.statusPill} ${s['statusPill--paused']}`
  if (sessionState === 'review' || sessionState === 'validated')
    return `${s.statusPill} ${s['statusPill--review']}`
  return `${s.statusPill} ${s['statusPill--idle']}`
}

// ── useTrainingWs ─────────────────────────────────────────────────────────────

/**
 * Opens / closes the WebSocket for a training session.
 * Sends binary frames to onFrame; lifecycle events to onOpen / onClose.
 * Returns a stable `send` function.
 */
function useTrainingWs(
  sessionId: string | null,
  callbacks: {
    onFrame(bytes: ArrayBuffer): void
    onOpen(): void
    onClose(): void
  },
): { send(msg: object): void; close(): void } {
  const wsRef    = useRef<WebSocket | null>(null)
  // Hold callbacks in a ref so the effect doesn't re-run when they change identity
  const cbRef    = useRef(callbacks)
  cbRef.current  = callbacks

  useEffect(() => {
    if (!sessionId) return
    const tok = token()
    if (!tok) return

    const ws = new WebSocket(wsUrl(sessionId, tok))
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onopen    = () => cbRef.current.onOpen()
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) cbRef.current.onFrame(ev.data)
    }
    ws.onclose   = () => cbRef.current.onClose()
    ws.onerror   = () => ws.close()

    return () => {
      ws.close()
      wsRef.current = null
    }
  }, [sessionId])

  const send = useCallback((msg: object) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg))
    }
  }, [])

  const close = useCallback(() => {
    wsRef.current?.close()
    wsRef.current = null
  }, [])

  return { send, close }
}

// ── useLiveCanvas ─────────────────────────────────────────────────────────────

const THROTTLE_MS = 50

/**
 * Renders JPEG frames onto the canvas element and produces pointer / keyboard
 * event handlers that map canvas coordinates → remote browser coordinates.
 *
 * Coordinate mapping:
 *   The remote browser is assumed to be ~1280×720 pixels.
 *   The canvas fills its display area with object-fit: contain semantics:
 *   we compute the letterbox offset and uniform scale factor on every frame
 *   from the drawn image's natural size, then apply the inverse transform
 *   to pointer events so the server sees browser-space coordinates.
 */
function useLiveCanvas(
  canvasRef: React.RefObject<HTMLCanvasElement | null>,
  send: (msg: object) => void,
) {
  // Letterbox geometry of the drawn frame IN BACKING-STORE (device) px. Clicks
  // are mapped to a NORMALIZED fraction of the drawn image so the server (which
  // owns the viewport) converts to CSS px — exact regardless of source resolution
  // or DPR (fixes the "click lands off left/right" imprecision).
  const layoutRef = useRef({ offX: 0, offY: 0, drawW: 1, drawH: 1 })
  const lastMoveAt = useRef(0)

  // Keep a stable renderFrame reference; only changes if canvasRef instance changes
  const renderFrame = useCallback((jpegBytes: ArrayBuffer) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const blob = new Blob([jpegBytes], { type: 'image/jpeg' })
    const objUrl = URL.createObjectURL(blob)
    const img = new Image()

    img.onload = () => {
      const srcW = img.naturalWidth  || 1600
      const srcH = img.naturalHeight || 900
      const dstW = canvas.width
      const dstH = canvas.height
      const scale = Math.min(dstW / srcW, dstH / srcH)
      const drawW = srcW * scale
      const drawH = srcH * scale
      const offX  = (dstW - drawW) / 2
      const offY  = (dstH - drawH) / 2

      layoutRef.current = { offX, offY, drawW, drawH }

      ctx.clearRect(0, 0, dstW, dstH)
      ctx.drawImage(img, offX, offY, drawW, drawH)
      URL.revokeObjectURL(objUrl)
    }
    img.onerror = () => URL.revokeObjectURL(objUrl)
    img.src = objUrl
  }, [canvasRef])

  function canvasToFraction(ev: React.MouseEvent<HTMLCanvasElement>): { xf: number; yf: number } {
    const canvas = canvasRef.current
    const { offX, offY, drawW, drawH } = layoutRef.current
    // offsetX/Y are CSS px; the letterbox geometry is in backing-store (device) px.
    // Scale the pointer to backing-store px via the canvas' own ratio, then express
    // it as a fraction of the DRAWN image. The server multiplies by the viewport.
    const ratioX = canvas && canvas.clientWidth ? canvas.width / canvas.clientWidth : 1
    const ratioY = canvas && canvas.clientHeight ? canvas.height / canvas.clientHeight : 1
    const px = ev.nativeEvent.offsetX * ratioX
    const py = ev.nativeEvent.offsetY * ratioY
    const clamp = (v: number) => Math.min(1, Math.max(0, v))
    return { xf: clamp((px - offX) / drawW), yf: clamp((py - offY) / drawH) }
  }

  const onMouseDown = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    const { xf, yf } = canvasToFraction(ev)
    send({ type: 'mouse', action: 'down', xf, yf, button: ev.button })
    canvasRef.current?.focus()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [send])

  const onMouseUp = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    const { xf, yf } = canvasToFraction(ev)
    send({ type: 'mouse', action: 'up', xf, yf, button: ev.button })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [send])

  const onMouseMove = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    const now = Date.now()
    if (now - lastMoveAt.current < THROTTLE_MS) return
    lastMoveAt.current = now
    const { xf, yf } = canvasToFraction(ev)
    send({ type: 'mouse', action: 'move', xf, yf, button: 0 })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [send])

  const onKeyDown = useCallback((ev: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) ev.preventDefault()
    send({ type: 'key', action: 'down', keysym: null, text: ev.key })
  }, [send])

  const onKeyUp = useCallback((ev: React.KeyboardEvent<HTMLCanvasElement>) => {
    send({ type: 'key', action: 'up', keysym: null, text: ev.key })
  }, [send])

  // Keep the canvas BACKING STORE in sync with its CSS display size × the
  // device pixel ratio. On a Retina display devicePixelRatio is 2, so a backing
  // store sized to CSS px alone is drawn at half resolution and the browser
  // upscales it → blurry. Sizing the buffer to rect × dpr makes the frame render
  // at native physical resolution (crisp). The CSS size is unchanged (the canvas
  // still fills its box); only the drawing buffer grows.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (!rect) return
      const dpr = window.devicePixelRatio || 1
      canvas.width  = Math.round(rect.width * dpr)
      canvas.height = Math.round(rect.height * dpr)
    })
    ro.observe(canvas)
    return () => ro.disconnect()
  }, [canvasRef])

  return { renderFrame, onMouseDown, onMouseUp, onMouseMove, onKeyDown, onKeyUp }
}

// ── TeachingView ──────────────────────────────────────────────────────────────

export default function TeachingView() {
  const [vs, dispatch] = useReducer(reducer, { phase: 'setup' })

  const [skillName, setSkillName] = useState('')
  const [startUrl,  setStartUrl]  = useState('https://')
  const [starting,  setStarting]  = useState(false)
  const [busy,      setBusy]      = useState(false)

  const canvasRef        = useRef<HTMLCanvasElement>(null)
  const canvasFrameRef   = useRef<HTMLDivElement>(null)
  const pendingNavRef    = useRef<string | null>(null)
  const [hasFrame,       setHasFrame]      = useState(false)
  // Non-null while the operator is watching the agent execute the taught skill
  // live (read-only). Holds the skill name being verified.
  const [verifying,      setVerifying]     = useState<string | null>(null)

  // Fullscreen the live browser frame. requestFullscreen on the FRAME container
  // (not the canvas) so the URL bar / hint stay visible; the ResizeObserver grows
  // the canvas backing store and normalized coords keep clicks exact.
  const toggleFullscreen = useCallback(() => {
    const el = canvasFrameRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen?.()
  }, [])

  // ── WS callbacks (stable refs inside the hook) ───────────────────────────

  const onFrame = useCallback((bytes: ArrayBuffer) => {
    renderFrame(bytes)
    setHasFrame(true)
  // renderFrame is stable (memoized inside useLiveCanvas)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onOpen = useCallback(() => {
    dispatch({ type: 'WS_OPEN' })
  }, [])

  const onClose = useCallback(() => {
    setHasFrame(false)
  }, [])

  const activeSessionId =
    vs.phase === 'live' || vs.phase === 'connecting'
      ? vs.session.sessionId
      : null

  const { send, close: closeWs } = useTrainingWs(activeSessionId, { onFrame, onOpen, onClose })

  const { renderFrame, onMouseDown, onMouseUp, onMouseMove, onKeyDown, onKeyUp } =
    useLiveCanvas(canvasRef, send)

  // Send the pending navigation once the WS is open
  const wsReady = vs.phase === 'live' && vs.wsReady
  useEffect(() => {
    if (!wsReady) return
    const navUrl = pendingNavRef.current
    if (navUrl) {
      send({ type: 'navigate', url: navUrl })
      pendingNavRef.current = null
    }
  }, [wsReady, send])

  // Poll session state while a session is active (every 3 s)
  useEffect(() => {
    if (!activeSessionId) return
    let alive = true
    const poll = async () => {
      try {
        const st = await getTrainingState(activeSessionId)
        if (alive) {
          dispatch({ type: 'REMOTE_STATE', sessionState: st.state as SessionPhaseState })
        }
      } catch {
        /* transient — keep last known state */
      }
    }
    const id = setInterval(poll, 3000)
    return () => { alive = false; clearInterval(id) }
  }, [activeSessionId])

  // Cleanup WS on unmount
  useEffect(() => {
    return () => { closeWs() }
  }, [closeWs])

  // ── Handlers ──────────────────────────────────────────────────────────────

  async function handleStart() {
    const name = skillName.trim()
    const url  = startUrl.trim()

    if (!name) { toastWarn('Ponle un nombre a la habilidad'); return }
    if (!url || url === 'https://') { toastWarn('Introduce una URL de inicio para la demostración'); return }

    setStarting(true)
    try {
      const sess = await createTrainingSession({
        skill_name: name,
        description: '',
        surface_kind: 'browser',
      })
      await startTrainingRecording(sess.session_id)

      pendingNavRef.current = url

      dispatch({
        type: 'START_CONNECTING',
        session: { sessionId: sess.session_id, skillName: name, sessionState: 'capturing' },
      })
    } catch (e) {
      toastErr(e instanceof ApiError ? e.message : 'No se pudo iniciar la grabación')
    } finally {
      setStarting(false)
    }
  }

  async function handlePause() {
    if (vs.phase !== 'live') return
    setBusy(true)
    try {
      await pauseTrainingRecording(vs.session.sessionId)
      dispatch({ type: 'REMOTE_STATE', sessionState: 'paused' })
    } catch (e) {
      toastErr(e instanceof ApiError ? e.message : 'No se pudo pausar')
    } finally {
      setBusy(false)
    }
  }

  async function handleResume() {
    if (vs.phase !== 'live') return
    setBusy(true)
    try {
      await resumeTrainingRecording(vs.session.sessionId)
      dispatch({ type: 'REMOTE_STATE', sessionState: 'capturing' })
    } catch (e) {
      toastErr(e instanceof ApiError ? e.message : 'No se pudo reanudar')
    } finally {
      setBusy(false)
    }
  }

  async function handleStop() {
    if (vs.phase !== 'live') return
    setBusy(true)
    closeWs()
    try {
      await stopTrainingRecording(vs.session.sessionId)
      dispatch({ type: 'STOP' })
      toastOk('Grabación detenida. Guarda la habilidad cuando estés listo.')
    } catch (e) {
      toastErr(e instanceof ApiError ? e.message : 'No se pudo detener la grabación')
    } finally {
      setBusy(false)
    }
  }

  async function handleSign() {
    if (vs.phase !== 'review') return
    const { sessionId, skillName: name } = vs.session
    setBusy(true)
    try {
      await signTrainingSession(sessionId)
      toastOk(`Habilidad "${name}" guardada. Ya puede usarla el agente.`)
      dispatch({ type: 'SIGN_OK' })
      setSkillName('')
      setStartUrl('https://')
    } catch (e) {
      toastErr(e instanceof ApiError ? e.message : 'No se pudo guardar la habilidad')
    } finally {
      setBusy(false)
    }
  }

  // Verificar: save the skill (if not yet), ask the agent (via chat) to USE it,
  // and open a read-only LIVE view of the agent's internal browser so the human
  // watches it execute in real time and corroborates it works.
  async function handleVerify() {
    const sid =
      vs.phase === 'review'     ? vs.session.sessionId :
      vs.phase === 'live'       ? vs.session.sessionId : null
    const name = (sessionInView?.skillName ?? '').trim()
    if (!name) { toastWarn('No hay habilidad que verificar'); return }
    setBusy(true)
    try {
      // Ensure the skill is saved before asking the agent to use it. Best-effort:
      // if it was already signed, the daemon call is a harmless no-op / error we
      // swallow.
      if (sid) { try { await signTrainingSession(sid) } catch { /* already saved */ } }
      await postChat({
        user_message: `Usa la habilidad "${name}" para hacer la tarea que te acabo de enseñar y muéstrame el resultado.`,
        dedup_key: `verify:${name}:${Date.now()}`,
      })
      closeWs()
      setHasFrame(false)
      setVerifying(name)
      dispatch({ type: 'SIGN_OK' })
      setSkillName('')
      setStartUrl('https://')
    } catch (e) {
      toastErr(e instanceof ApiError ? e.message : 'No se pudo lanzar la verificación')
    } finally {
      setBusy(false)
    }
  }

  async function handleCancel() {
    const sid =
      vs.phase === 'live'       ? vs.session.sessionId :
      vs.phase === 'connecting' ? vs.session.sessionId :
      vs.phase === 'review'     ? vs.session.sessionId :
      null

    closeWs()
    setHasFrame(false)

    if (sid) {
      try { await cancelTrainingRecording(sid) } catch { /* ignore — best-effort */ }
    }

    dispatch({ type: 'CANCEL' })
  }

  function handleNavigate(url: string) {
    send({ type: 'navigate', url })
  }

  // ── Derived booleans for rendering ────────────────────────────────────────

  const sessionInView =
    vs.phase === 'live'       ? vs.session :
    vs.phase === 'connecting' ? vs.session :
    vs.phase === 'review'     ? vs.session :
    null

  const isLive        = vs.phase === 'live'
  const isConnecting  = vs.phase === 'connecting'
  const isReview      = vs.phase === 'review'
  const isCapturing   = isLive && vs.session.sessionState === 'capturing'
  const isPaused      = isLive && vs.session.sessionState === 'paused'
  const controlsBusy  = busy || starting

  return (
    <>
      <PageHeader
        title="Modo Enseñanza"
        subtitle="Demuestra una tarea en el navegador aislado. Lumen observa e interpreta los pasos para crear una habilidad reutilizable."
      />

      <div className={s.viewBody}>

        {/* ── Error banner ─────────────────────────────────────────────── */}
        {vs.phase === 'error' && (
          <FadeIn>
            <div role="alert" className={s.errorBanner}>
              <span className={s.errorBannerIcon} aria-hidden="true">
                <AlertTriangle size={16} />
              </span>
              <div className={s.errorBannerBody}>
                <p className={s.errorBannerTitle}>Algo salió mal</p>
                <p className={s.errorBannerDesc}>{vs.message}</p>
                <div className={s.errorBannerActions}>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => dispatch({ type: 'CANCEL' })}
                  >
                    Volver al inicio
                  </Button>
                </div>
              </div>
            </div>
          </FadeIn>
        )}

        {/* ── Verify live-watch (read-only view of the agent executing) ──── */}
        {verifying && (
          <VerifyWatch skillName={verifying} onClose={() => setVerifying(null)} />
        )}

        {/* ── Setup card ───────────────────────────────────────────────── */}
        {vs.phase === 'setup' && !verifying && (
          <FadeIn>
            <div className={s.setupCard}>
              <div>
                <p className={s.setupTitle}>Nueva demostración</p>
                <p className={s.setupDesc}>
                  Introduce el nombre de la habilidad y la URL de inicio. El agente capturará
                  tus interacciones en tiempo real y las convertirá en pasos reutilizables.
                </p>
              </div>

              <div>
                <label htmlFor="teach-name" className="sr-only">
                  Nombre de la habilidad
                </label>
                <input
                  id="teach-name"
                  className={s.urlInput}
                  type="text"
                  placeholder='Nombre (p. ej. "Publicar en LinkedIn")'
                  autoComplete="off"
                  value={skillName}
                  onChange={(e) => setSkillName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void handleStart() }}
                />
              </div>

              <div className={s.urlRow}>
                <label htmlFor="teach-start-url" className="sr-only">
                  URL de inicio
                </label>
                <div className={s.urlWrap}>
                  <input
                    id="teach-start-url"
                    className={s.urlInput}
                    type="url"
                    placeholder="https://ejemplo.com"
                    autoComplete="url"
                    value={startUrl}
                    onChange={(e) => setStartUrl(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void handleStart() }}
                  />
                </div>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => void handleStart()}
                  loading={starting}
                  disabled={starting}
                >
                  Empezar
                </Button>
              </div>
            </div>
          </FadeIn>
        )}

        {/* ── Session controls bar ──────────────────────────────────────── */}
        {sessionInView && (
          <FadeIn key="controls-bar">
            <div
              className={s.controlsBar}
              role="toolbar"
              aria-label="Controles de la sesión de grabación"
            >
              <span
                className={pillClass(sessionInView.sessionState)}
                role="status"
                aria-live="polite"
                aria-atomic="true"
              >
                {isCapturing && <span className={s.recordingDot} aria-hidden="true" />}
                {sessionStateLabel(sessionInView.sessionState)}
              </span>

              <span
                style={{
                  fontSize: 'var(--text-sm)',
                  color: 'var(--color-text-muted)',
                  fontWeight: 'var(--weight-medium)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  maxWidth: '220px',
                }}
              >
                {sessionInView.skillName}
              </span>

              <div style={{ flex: 1 }} />

              {isCapturing && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void handlePause()}
                  disabled={controlsBusy}
                >
                  Pausar
                </Button>
              )}

              {isPaused && (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => void handleResume()}
                  disabled={controlsBusy}
                >
                  Reanudar
                </Button>
              )}

              {isLive && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void handleStop()}
                  loading={busy && !isCapturing && !isPaused}
                  disabled={controlsBusy}
                >
                  Detener
                </Button>
              )}

              {isReview && (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => void handleSign()}
                  loading={busy}
                  disabled={controlsBusy}
                >
                  Guardar habilidad
                </Button>
              )}

              {isReview && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void handleVerify()}
                  disabled={controlsBusy}
                  aria-label="Guardar y verificar la habilidad en vivo"
                >
                  Verificar
                </Button>
              )}

              {(isLive || isConnecting || isReview) && (
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => void handleCancel()}
                  disabled={controlsBusy}
                >
                  Cancelar
                </Button>
              )}
            </div>
          </FadeIn>
        )}

        {/* ── Live canvas ───────────────────────────────────────────────── */}
        {(isLive || isConnecting) && (
          <FadeIn key="live-area">
            <div className={s.canvasSection}>

              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                <span className={s.canvasLabel}>Navegador en vivo</span>
                <div style={{ flex: 1 }} />
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={toggleFullscreen}
                  aria-label="Pantalla completa"
                >
                  <Maximize2 size={13} aria-hidden="true" />
                  Pantalla completa
                </Button>
              </div>

              {/* Inline navigation bar for the live session */}
              <LiveUrlBar
                initialUrl={startUrl}
                disabled={isPaused || isConnecting}
                onNavigate={handleNavigate}
              />

              <p className={s.canvasHint}>
                <strong>Haz clic en el área de abajo para enfocarla</strong>, luego interactúa
                con el teclado y el ratón. Las pulsaciones de tecla van al navegador demostrado
                mientras esté enfocado.
              </p>

              <div className={s.canvasFrame} ref={canvasFrameRef}>
                <canvas
                  ref={canvasRef}
                  className={s.liveCanvas}
                  tabIndex={0}
                  aria-label={
                    hasFrame
                      ? 'Navegador en vivo. Haz clic para interactuar. Las pulsaciones de tecla van al navegador mientras esté enfocado.'
                      : 'Área del navegador en vivo — esperando conexión'
                  }
                  onMouseDown={onMouseDown}
                  onMouseUp={onMouseUp}
                  onMouseMove={onMouseMove}
                  onKeyDown={onKeyDown}
                  onKeyUp={onKeyUp}
                  onContextMenu={(e) => e.preventDefault()}
                />

                {!hasFrame && (
                  <div className={s.canvasOverlay} aria-live="polite" aria-atomic="true">
                    <span className={s.overlayIcon} aria-hidden="true">
                      <Monitor size={32} />
                    </span>
                    {isConnecting
                      ? 'Conectando con el navegador aislado…'
                      : 'Esperando el primer fotograma…'}
                  </div>
                )}
              </div>
            </div>
          </FadeIn>
        )}

        {/* ── Review state ─────────────────────────────────────────────── */}
        {isReview && (
          <FadeIn key="review-state">
            <EmptyState
              icon={<Monitor size={28} />}
              title="Grabación completada"
              description="La sesión se ha detenido. Guarda la habilidad para que el agente la aprenda, o descártala si quieres repetir la demostración."
              action={
                <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() => void handleSign()}
                    loading={busy}
                    disabled={controlsBusy}
                  >
                    Guardar habilidad
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => void handleVerify()}
                    disabled={controlsBusy}
                  >
                    Verificar en vivo
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void handleCancel()}
                    disabled={controlsBusy}
                  >
                    Descartar
                  </Button>
                </div>
              }
            />
          </FadeIn>
        )}

        {/* ── Setup idle hint ───────────────────────────────────────────── */}
        {vs.phase === 'setup' && !verifying && (
          <FadeIn key="setup-hint">
            <EmptyState
              compact
              icon={<Monitor size={28} />}
              title="Sin sesión activa"
              description="Rellena el formulario de arriba para iniciar una nueva demostración. El navegador aislado aparecerá aquí en tiempo real."
            />
          </FadeIn>
        )}

      </div>
    </>
  )
}

// ── LiveUrlBar ────────────────────────────────────────────────────────────────

/**
 * Small URL bar shown above the live canvas so the operator can navigate
 * to different pages without leaving the TeachingView.
 */
interface LiveUrlBarProps {
  initialUrl: string
  disabled: boolean
  onNavigate(url: string): void
}

function LiveUrlBar({ initialUrl, disabled, onNavigate }: LiveUrlBarProps) {
  const [value, setValue] = useState(initialUrl)

  function commit() {
    const url = value.trim()
    if (url) onNavigate(url)
  }

  return (
    <div className={s.urlRow}>
      <label htmlFor="live-nav-url" className="sr-only">
        Barra de dirección del navegador en vivo
      </label>
      <div className={s.urlWrap}>
        <input
          id="live-nav-url"
          className={s.urlInput}
          type="url"
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commit() } }}
          aria-label="URL del navegador en vivo"
        />
      </div>
      <Button
        variant="secondary"
        size="sm"
        disabled={disabled}
        onClick={commit}
      >
        Ir
      </Button>
    </div>
  )
}

// ── VerifyWatch ─────────────────────────────────────────────────────────────────

/**
 * Read-only LIVE view of the AGENT's internal browser while it executes the
 * just-taught skill. Streams JPEG frames from WS /api/v1/watch/agent/live and
 * renders them (DPR-aware, letterboxed) — no input injection. The human watches
 * and corroborates the run.
 */
function VerifyWatch({ skillName, onClose }: { skillName: string; onClose: () => void }) {
  const navigate = useNavigate()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const frameRef = useRef<HTMLDivElement>(null)
  const [hasFrame, setHasFrame] = useState(false)

  const renderFrame = useCallback((bytes: ArrayBuffer) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const objUrl = URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' }))
    const img = new Image()
    img.onload = () => {
      const srcW = img.naturalWidth || 1600
      const srcH = img.naturalHeight || 900
      const dstW = canvas.width
      const dstH = canvas.height
      const scale = Math.min(dstW / srcW, dstH / srcH)
      const drawW = srcW * scale
      const drawH = srcH * scale
      const offX = (dstW - drawW) / 2
      const offY = (dstH - drawH) / 2
      ctx.clearRect(0, 0, dstW, dstH)
      ctx.drawImage(img, offX, offY, drawW, drawH)
      URL.revokeObjectURL(objUrl)
    }
    img.onerror = () => URL.revokeObjectURL(objUrl)
    img.src = objUrl
  }, [])

  // Keep the canvas backing store at physical resolution (crisp on Retina).
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (!rect) return
      const dpr = window.devicePixelRatio || 1
      canvas.width = Math.round(rect.width * dpr)
      canvas.height = Math.round(rect.height * dpr)
    })
    ro.observe(canvas)
    return () => ro.disconnect()
  }, [])

  // Open the read-only watch WebSocket.
  useEffect(() => {
    const tok = token()
    if (!tok) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(
      `${proto}//${location.host}/api/v1/watch/agent/live?token=${encodeURIComponent(tok)}`,
    )
    ws.binaryType = 'arraybuffer'
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) { renderFrame(ev.data); setHasFrame(true) }
    }
    ws.onerror = () => ws.close()
    return () => ws.close()
  }, [renderFrame])

  const toggleFullscreen = useCallback(() => {
    const el = frameRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen?.()
  }, [])

  return (
    <FadeIn key="verify-area">
      <div className={s.canvasSection}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
          <span className={s.canvasLabel}>Verificando: {skillName}</span>
          <div style={{ flex: 1 }} />
          <Button variant="secondary" size="sm" onClick={toggleFullscreen} aria-label="Pantalla completa">
            <Maximize2 size={13} aria-hidden="true" />
            Pantalla completa
          </Button>
          <Button variant="secondary" size="sm" onClick={() => navigate('/chat')} aria-label="Abrir el chat de la tarea">
            <MessageSquare size={13} aria-hidden="true" />
            Abrir chat
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Cerrar verificación">
            <X size={13} aria-hidden="true" />
            Cerrar
          </Button>
        </div>

        <p className={s.canvasHint}>
          Lumen está ejecutando la habilidad en el navegador interno. Obsérvalo y corrobora
          que funciona; sigue el detalle en el chat.
        </p>

        <div className={s.canvasFrame} ref={frameRef}>
          <canvas
            ref={canvasRef}
            className={s.liveCanvas}
            style={{ cursor: 'default' }}
            aria-label="Navegador del agente en vivo (solo lectura)"
          />
          {!hasFrame && (
            <div className={s.canvasOverlay} aria-live="polite" aria-atomic="true">
              <span className={s.overlayIcon} aria-hidden="true">
                <Monitor size={32} />
              </span>
              Esperando a que el agente abra el navegador…
            </div>
          )}
        </div>
      </div>
    </FadeIn>
  )
}
