/*
 * VncView — sharp + fluid live view of the jailed browser via noVNC.
 *
 * The jailed Chromium runs HEADFUL on an Xvfb display; x11vnc serves it and the
 * shell-server bridges RFB over a WebSocket at /api/v1/vnc. noVNC connects there and
 * renders the REAL display pixels (industry-standard live-view, Kasm/neko) — no more
 * blurry CDP screencast / slow captureScreenshot. viewOnly=true for Actividad
 * (watch the agent), false for Enseñar (drive it to demonstrate a skill).
 *
 * Clipboard (interactive only) — the OS-edition model, adapted with xclip. x11vnc's own
 * clipboard is broken for a jailed Chromium (it never answers the TARGETS request
 * Chromium sends before pasting → paste hangs; and it never emits ServerCutText → copy
 * is dead). So we bypass it:
 *   PASTE  (outside → jail): intercept Cmd/Ctrl+V in capture, read the local clipboard,
 *     POST it to the jail's clipboard server (xclip becomes the X CLIPBOARD owner and
 *     answers TARGETS), THEN inject a REAL Ctrl+V over RFB (rfb.sendKey) so the focused
 *     app pastes it. Order matters: the clipboard must be set before the paste fires.
 *   COPY   (jail → outside): Ctrl+C is NOT intercepted — it reaches the app normally and
 *     copies to the X CLIPBOARD; a 1.5s poll (+ on focus) reads it back via the bridge
 *     and writes it to the local clipboard. UTF-8 (accents/€/emoji/CJK) works throughout.
 *   macOS Cmd combos: noVNC forwards a held Cmd to the jail as Alt/Super (RealVNC
 *     convention), so Cmd+chords die inside the jail. We release those stuck modifiers
 *     before every injection and translate Cmd+C/X/A into injected Ctrl+chords; Cmd+V is
 *     the paste flow above. Local clipboard access goes through the Tauri host bridge
 *     when the UI runs in the desktop shell (no WKWebView paste-permission prompt) and
 *     falls back to navigator.clipboard in a plain browser.
 */
import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { useT } from '../lib/i18n'
import { token } from '../lib/token'
import { getBrowserClipboard, setBrowserClipboard } from '../api/client'

type Status = 'connecting' | 'connected' | 'disconnected'

// X11 keysyms for the clean, real Ctrl+chord injection over RFB.
const XK_Control_L = 0xffe3
const XK_Shift_L = 0xffe1
const XK_v = 0x0076
const XK_c = 0x0063
const XK_x = 0x0078
const XK_a = 0x0061
// Meta-ish modifiers the jail may believe are held (see releaseStuckModifiers).
const XK_Meta_L = 0xffe7
const XK_Meta_R = 0xffe8
const XK_Alt_L = 0xffe9
const XK_Alt_R = 0xffea
const XK_Super_L = 0xffeb
const XK_Super_R = 0xffec

const isMacLike = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform || '')

function isPasteCombo(e: KeyboardEvent): boolean {
  const v = e.key === 'v' || e.key === 'V' || e.keyCode === 86
  return v && (e.ctrlKey || e.metaKey) && !e.altKey
}

// ---- local clipboard access (desktop app vs plain browser) ---------------------------
// Under the Tauri desktop shell, navigator.clipboard.readText() is gated by the webview
// (WKWebView pops a paste-permission "Paste" button; WebKitGTK may deny silently), which
// killed paste-into-Live since the app shipped as a .dmg. The shell exposes the HOST
// clipboard over IPC (read_host_clipboard / write_host_clipboard — granted to this origin
// in the shell's capabilities), so when Tauri is present we use that: no prompt, and it is
// the same clipboard the user actually copied into. In a plain browser (curl install) we
// keep navigator.clipboard, which works natively there.
type TauriGlobal = { core?: { invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> } }

function tauriInvoke(): ((cmd: string, args?: Record<string, unknown>) => Promise<unknown>) | null {
  const t = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__
  return t?.core?.invoke ?? null
}

function readLocalClipboard(): Promise<string> {
  const invoke = tauriInvoke()
  if (invoke) {
    return invoke('read_host_clipboard')
      .then((v) => (typeof v === 'string' ? v : ''))
      .catch(() => navigator.clipboard?.readText?.() ?? '')
  }
  return navigator.clipboard.readText()
}

function writeLocalClipboard(text: string): Promise<void> {
  const invoke = tauriInvoke()
  if (invoke) {
    return invoke('write_host_clipboard', { text })
      .then(() => undefined)
      .catch(() => navigator.clipboard?.writeText?.(text))
  }
  return navigator.clipboard.writeText(text)
}

/** Framed container around a VncView — used by Actividad and the chat inline live panel
 *  (16:9), and the full-screen teaching modal (fill = grow to fill the flex parent). */
export function VncFrame({ viewOnly, fill }: { viewOnly?: boolean; fill?: boolean }) {
  return (
    <div
      style={{
        position: 'relative',
        width: '100%',
        ...(fill
          ? { flex: 1, minHeight: 0 }
          : { aspectRatio: '16 / 9', maxHeight: 'min(74vh, 900px)' }),
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
  const t = useT()
  const ref = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<Status>('connecting')

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${location.host}/api/v1/vnc?token=${encodeURIComponent(token() || '')}`
    let rfb: RFB | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let poll: ReturnType<typeof setInterval> | null = null

    const canRead = tauriInvoke() != null || !!(navigator.clipboard && navigator.clipboard.readText)
    let lastPushed: string | null = null // last text we set on the jail clipboard
    let lastSeenFromJail: string | null = null // last text we pulled from the jail

    // noVNC forwards a held Cmd to the jail as Alt_L (left Cmd) / Super_L (right Cmd) —
    // the RealVNC/TigerVNC modifier convention. So when the user triggers Cmd+V, by
    // injection time the jail believes Alt/Super is DOWN and would read our Ctrl+V as
    // Ctrl+Alt+V (not a paste) — the historic reason Cmd+combos "never worked" in Live
    // while Ctrl+combos did. Release every meta-ish modifier first: spurious keyups are
    // harmless in X11, and the later physical Cmd release just repeats one.
    const releaseStuckModifiers = () => {
      if (!rfb) return
      rfb.sendKey(XK_Alt_L, 'AltLeft', false)
      rfb.sendKey(XK_Alt_R, 'AltRight', false)
      rfb.sendKey(XK_Super_L, 'MetaLeft', false)
      rfb.sendKey(XK_Super_R, 'MetaRight', false)
      rfb.sendKey(XK_Meta_L, 'MetaLeft', false)
      rfb.sendKey(XK_Meta_R, 'MetaRight', false)
    }

    // Inject a clean, REAL Ctrl+V (or Ctrl+Shift+V) to the jailed app over RFB.
    const injectPaste = (withShift: boolean) => {
      if (!rfb) return
      releaseStuckModifiers()
      rfb.sendKey(XK_Control_L, 'ControlLeft', true)
      if (withShift) rfb.sendKey(XK_Shift_L, 'ShiftLeft', true)
      rfb.sendKey(XK_v, 'KeyV', true)
      rfb.sendKey(XK_v, 'KeyV', false)
      if (withShift) rfb.sendKey(XK_Shift_L, 'ShiftLeft', false)
      rfb.sendKey(XK_Control_L, 'ControlLeft', false)
    }

    // Inject Ctrl+<key> — used to translate macOS Cmd+C/X/A into the chord the jailed
    // LINUX Chromium actually understands.
    const injectCtrlChord = (keysym: number, code: string) => {
      if (!rfb) return
      releaseStuckModifiers()
      rfb.sendKey(XK_Control_L, 'ControlLeft', true)
      rfb.sendKey(keysym, code, true)
      rfb.sendKey(keysym, code, false)
      rfb.sendKey(XK_Control_L, 'ControlLeft', false)
    }

    // Outside → jail: set the jail clipboard from the local one, THEN inject Ctrl+V so
    // the focused app pastes it. Intercept in capture, before noVNC forwards the key.
    // Also: on macOS, Cmd+C/X/A are translated to the Ctrl+chord the jailed Linux
    // Chromium expects (noVNC would forward them as Alt/Super+key — dead keys there).
    // preventDefault() additionally stops WebKit from re-dispatching the unhandled combo
    // to the app menu, so the app's Edit menu never fires while Live owns the keys.
    const onKeyDownCapture = (e: KeyboardEvent) => {
      if (viewOnly || !rfb) return
      if (isPasteCombo(e)) {
        if (!canRead) return
        e.preventDefault()
        e.stopImmediatePropagation()
        const withShift = e.shiftKey
        readLocalClipboard()
          .then((text) => {
            // '' means empty/non-text local clipboard (e.g. a copied image) — don't wipe
            // the jail's clipboard with it; paste whatever the jail already has.
            if (text && text !== lastPushed) {
              lastPushed = text
              return setBrowserClipboard(text)
            }
          })
          .catch(() => { /* no permission/empty → paste whatever the jail already has */ })
          .then(() => injectPaste(withShift))
        return
      }
      if (isMacLike && e.metaKey && !e.ctrlKey && !e.altKey) {
        const k = (e.key || '').toLowerCase()
        const chord =
          k === 'c' ? ([XK_c, 'KeyC'] as const)
          : k === 'x' ? ([XK_x, 'KeyX'] as const)
          : k === 'a' ? ([XK_a, 'KeyA'] as const)
          : null
        if (chord) {
          e.preventDefault()
          e.stopImmediatePropagation()
          injectCtrlChord(chord[0], chord[1])
          // Copy/cut results flow back via the existing jail-clipboard poll (pullFromJail).
        }
      }
    }

    // Jail → outside: mirror the jail clipboard into the local one (dedup vs our push).
    const pullFromJail = () => {
      getBrowserClipboard()
        .then((r) => {
          const t = r?.text
          if (!t || t === lastSeenFromJail || t === lastPushed) return
          lastSeenFromJail = t
          writeLocalClipboard(t).catch(() => {
            setTimeout(() => writeLocalClipboard(t).catch(() => {}), 0)
          })
        })
        .catch(() => { /* transient */ })
    }

    // Keep the noVNC canvas focused so the device keyboard reaches the jailed browser.
    // noVNC binds keydown to its internal <canvas tabIndex=-1> and only focuses it on a
    // direct mousedown on the video — so typing was dead until you clicked EXACTLY on it.
    // We focus on connect and re-assert on pointerdown (NOT hover: that would steal focus
    // from the skill-name input while typing).
    const onPointerDown = () => { try { rfb?.focus() } catch { /* noop */ } }

    // On focus: presync the local clipboard to the jail so any later paste is correct.
    const onFocus = () => {
      if (canRead && document.hasFocus()) {
        readLocalClipboard()
          .then((text) => {
            if (text && text !== lastPushed) {
              lastPushed = text
              return setBrowserClipboard(text)
            }
          })
          .catch(() => { /* no permission yet */ })
      }
      pullFromJail()
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
        rfb.addEventListener('connect', () => {
          setStatus('connected')
          if (!viewOnly) { try { rfb?.focus() } catch { /* noop */ } }
        })
        rfb.addEventListener('disconnect', () => {
          setStatus('disconnected')
          retry = setTimeout(() => { try { rfb?.disconnect() } catch { /* noop */ } connect() }, 2500)
        })
      } catch {
        setStatus('disconnected')
      }
    }
    connect()

    if (!viewOnly) {
      el.addEventListener('keydown', onKeyDownCapture, true)
      el.addEventListener('pointerdown', onPointerDown, true)
      window.addEventListener('focus', onFocus)
      poll = setInterval(pullFromJail, 1500)
    }

    return () => {
      if (retry) clearTimeout(retry)
      if (poll) clearInterval(poll)
      if (!viewOnly) {
        el.removeEventListener('keydown', onKeyDownCapture, true)
        el.removeEventListener('pointerdown', onPointerDown, true)
        window.removeEventListener('focus', onFocus)
      }
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
          {status === 'connecting' ? t('vnc.connecting') : t('vnc.reconnecting')}
        </div>
      )}
    </div>
  )
}
