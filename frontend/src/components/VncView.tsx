/*
 * VncView — sharp + fluid live view of the jailed browser via noVNC.
 *
 * The jailed Chromium runs HEADFUL on an Xvfb display; x11vnc serves it and the
 * shell-server bridges RFB over a WebSocket at /api/v1/vnc. noVNC connects there and
 * renders the REAL display pixels (industry-standard live-view, Kasm/neko) — no more
 * blurry CDP screencast / slow captureScreenshot. viewOnly=true for Actividad
 * (watch the agent), false for Enseñar (drive it to demonstrate a skill).
 */
import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { token } from '../lib/token'

type Status = 'connecting' | 'connected' | 'disconnected'

export function VncView({
  viewOnly = false,
  className,
}: {
  viewOnly?: boolean
  className?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<Status>('connecting')

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${location.host}/api/v1/vnc?token=${encodeURIComponent(token() || '')}`
    let rfb: RFB | null = null
    let retry: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      setStatus('connecting')
      try {
        rfb = new RFB(el, url, { shared: true })
        rfb.viewOnly = viewOnly
        rfb.scaleViewport = true // fit the HiDPI framebuffer into the panel, crisp
        rfb.resizeSession = false
        rfb.focusOnClick = !viewOnly
        rfb.background = '#0a0a0a'
        rfb.addEventListener('connect', () => setStatus('connected'))
        rfb.addEventListener('disconnect', () => {
          setStatus('disconnected')
          // the jailed browser may still be cold-starting → retry a couple times
          retry = setTimeout(() => {
            try { rfb?.disconnect() } catch { /* noop */ }
            connect()
          }, 2500)
        })
      } catch {
        setStatus('disconnected')
      }
    }
    connect()

    return () => {
      if (retry) clearTimeout(retry)
      try { rfb?.disconnect() } catch { /* noop */ }
    }
  }, [viewOnly])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }} className={className}>
      <div ref={ref} style={{ width: '100%', height: '100%' }} />
      {status !== 'connected' && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--color-text-dim)',
            fontSize: 'var(--text-sm)',
            pointerEvents: 'none',
          }}
        >
          {status === 'connecting' ? 'Conectando al navegador…' : 'Reconectando…'}
        </div>
      )}
    </div>
  )
}
