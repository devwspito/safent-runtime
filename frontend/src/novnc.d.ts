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
    addEventListener(type: string, listener: (e: CustomEvent) => void): void
    removeEventListener(type: string, listener: (e: CustomEvent) => void): void
  }
}
