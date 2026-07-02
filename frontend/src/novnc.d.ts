// Minimal typing for @novnc/novnc (ships no .d.ts). Only the surface VncView uses.
declare module '@novnc/novnc' {
  export interface RFBOptions {
    credentials?: { username?: string; password?: string; target?: string }
    shared?: boolean
    wsProtocols?: string[]
  }
  export default class RFB {
    constructor(target: HTMLElement, url: string, options?: RFBOptions)
    viewOnly: boolean
    scaleViewport: boolean
    resizeSession: boolean
    background: string
    clipViewport: boolean
    focusOnClick: boolean
    disconnect(): void
    focus(): void
    // Set the REMOTE clipboard (sends RFB ClientCutText → x11vnc X selection).
    clipboardPasteFrom(text: string): void
    // Inject a key by X11 keysym (+ optional DOM code) — used to fire a clean Ctrl+V.
    sendKey(keysym: number, code: string | null, down?: boolean): void
    // 'clipboard' event fires when the REMOTE clipboard changes: detail.text.
    addEventListener(type: 'clipboard', listener: (e: CustomEvent<{ text: string }>) => void): void
    addEventListener(type: string, listener: (e: CustomEvent) => void): void
    removeEventListener(type: string, listener: (e: CustomEvent) => void): void
  }
}
