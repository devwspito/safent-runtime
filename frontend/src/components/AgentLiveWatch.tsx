/**
 * AgentLiveWatch — read-only LIVE view of the agent's internal jailed browser.
 *
 * Streams JPEG frames from WS /api/v1/watch/agent/live and renders them
 * (DPR-aware, letterboxed) onto a canvas. No input injection — the operator
 * watches the agent execute. Used by the "En vivo" Actividad panel and the
 * teaching Verificar flow.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Maximize2, Monitor } from 'lucide-react'
import { token } from '../lib/token'
import { Button } from './ui/Button'
import s from '../views/TeachingView.module.css'

export function AgentLiveWatch({ label = 'Navegador del agente en vivo' }: { label?: string }) {
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
    <div className={s.canvasSection}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
        <span className={s.canvasLabel}>{label}</span>
        <div style={{ flex: 1 }} />
        <Button variant="secondary" size="sm" onClick={toggleFullscreen} aria-label="Pantalla completa">
          <Maximize2 size={13} aria-hidden="true" />
          Pantalla completa
        </Button>
      </div>
      <div className={s.canvasFrame} ref={frameRef}>
        <canvas
          ref={canvasRef}
          className={s.liveCanvas}
          style={{ cursor: 'default' }}
          aria-label={`${label} (solo lectura)`}
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
  )
}
