/*
 * VncView — sharp + fluid live view of the jailed browser via noVNC.
 *
 * The jailed Chromium runs HEADFUL on an Xvfb display; x11vnc serves it and the
 * shell-server bridges RFB over a WebSocket at /api/v1/vnc. noVNC connects there and
 * renders the REAL display pixels (industry-standard live-view, Kasm/neko) — no more
 * blurry CDP screencast / slow captureScreenshot. viewOnly=true for Actividad
 * (watch the agent), false for Enseñar (drive it to demonstrate a skill).
 *
 * Clipboard (interactive only): UTF-8 copy/paste with an external source works via a
 * CDP bridge (clipboardPasteToBrowser / clipboardCopyFromBrowser), NOT x11vnc — its
 * clipboard is broken for a jailed Chromium (it never answers the TARGETS request
 * Chromium sends before pasting, so paste hangs; and it never emits ServerCutText, so
 * copy-out is dead). We intercept Ctrl/Cmd+V and +C here BEFORE noVNC forwards them to
 * RFB, and drive the jailed browser over CDP instead: paste = Input.insertText (UTF-8
 * native), copy = read window.getSelection(). Cmd→Ctrl handled by the combo checks.
 */
import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { token } from '../lib/token'
import { clipboardPasteToBrowser, clipboardCopyFromBrowser } from '../api/client'

type Status = 'connecting' | 'connected' | 'disconnected'

function isPasteCombo(e: KeyboardEvent): boolean {
  const v = e.key === 'v' || e.key === 'V' || e.keyCode === 86
  return v && (e.ctrlKey || e.metaKey) && !e.altKey
}

function isCopyCombo(e: KeyboardEvent): boolean {
  const c = e.key === 'c' || e.key === 'C' || e.keyCode === 67
  return c && (e.ctrlKey || e.metaKey) && !e.altKey
}

/** Framed 16:9 container around a VncView — used by Enseñar (interactive), Actividad
 *  and the chat inline live panel. */
export function VncFrame({ viewOnly }: { viewOnly?: boolean }) {
  return (
    <div
      style={{
        position: 'relative',
        width: '100%',
        aspectRatio: '16 / 9',
        maxHeight: 'min(74vh, 900px)',
        background: '#000',
        border: '1px solid var(--color-border-subtle)',
        borderRadius: 'var(--radius-md)',
        overflow: 'hidden',
      }}
    >
      <VncView viewOnly={viewOnly} />
    </div>
  )
}

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

    const canRead = !!(navigator.clipboard && navigator.clipboard.readText)

    // Outside → jail: read the local clipboard, insert it into the jailed browser over
    // CDP (UTF-8). Intercept BEFORE noVNC forwards the keystroke to RFB so the real
    // Ctrl+V never reaches x11vnc (whose paste would hang).
    const onKeyDownCapture = (e: KeyboardEvent) => {
      if (viewOnly) return
      if (isPasteCombo(e)) {
        if (!canRead) return
        e.preventDefault()
        e.stopImmediatePropagation()
        navigator.clipboard
          .readText()
          .then((text) => { if (text) return clipboardPasteToBrowser(text) })
          .catch(() => { /* no permission/empty → nothing to paste */ })
        return
      }
      if (isCopyCombo(e)) {
        // Jail → outside: read the jailed browser's current selection over CDP and
        // put it on the local clipboard. The remote selection was made with the mouse.
        e.preventDefault()
        e.stopImmediatePropagation()
        clipboardCopyFromBrowser()
          .then((r) => {
            const t = r?.text
            if (t) return navigator.clipboard?.writeText(t)
          })
          .catch(() => { /* transient — ignore */ })
      }
    }

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
          retry = setTimeout(() => { try { rfb?.disconnect() } catch { /* noop */ } connect() }, 2500)
        })
      } catch {
        setStatus('disconnected')
      }
    }
    connect()

    if (!viewOnly) el.addEventListener('keydown', onKeyDownCapture, true)

    return () => {
      if (retry) clearTimeout(retry)
      if (!viewOnly) el.removeEventListener('keydown', onKeyDownCapture, true)
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
