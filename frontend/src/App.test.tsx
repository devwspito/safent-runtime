import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Structural test for 028 FR-012/FR-013 (SC-012): the app shell must not
// issue a single authenticated request before a bearer is known good, and a
// stale bearer must cost exactly one refresh attempt — never a storm of
// 401s or a background retry loop. Mounts the REAL App (real client.ts, real
// token.ts) and only mocks fetch, so this exercises the actual production
// wiring end to end, not a stand-in.

// jsdom has no matchMedia — sileo's <Toaster> (mounted by App unconditionally)
// reads it for theme detection. Stub once, module-scoped, like any other
// missing-browser-API shim a test file owns for what it mounts.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

function setInjectedToken(value: string | undefined) {
  const w = window as unknown as Record<string, unknown>
  if (value === undefined) delete w['__SAFENT_TOKEN__']
  else w['__SAFENT_TOKEN__'] = value
}

function refreshCalls(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter(([url]) => String(url).includes('/session/refresh')).length
}

describe('App shell — auth gate (028 FR-012/FR-013)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(async () => {
    vi.resetModules()
    localStorage.clear()
    sessionStorage.clear()
    setInjectedToken(undefined)
    // App mounts <BrowserRouter basename="/app">, matching the shell-server
    // mount point — align jsdom's URL so the routed shell actually renders
    // (otherwise the authenticated-but-stale case never reaches Layout).
    window.history.pushState({}, '', '/app/chat')
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('issues ZERO /api/v1/* requests and shows the reconnect screen when no bearer exists at all', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const { default: App } = await import('./App')
    await act(async () => {
      root.render(React.createElement(App))
      await Promise.resolve()
    })

    expect(fetchMock).not.toHaveBeenCalled()
    expect(container.textContent).toContain('Reabre Safent')
    // The routed shell (sidebar wordmark) never mounted.
    expect(container.querySelector('nav')).toBeNull()
  })

  it('a stale bearer costs exactly ONE refresh attempt, then the reconnect screen — no request storm', async () => {
    vi.useFakeTimers()
    setInjectedToken('stale-secret')
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'unauthorized' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const { default: App } = await import('./App')
    await act(async () => {
      root.render(React.createElement(App))
    })
    // Flush the microtask queue repeatedly so every hook's mount-time fetch
    // (and the single refresh it triggers) settles before we assert.
    await act(async () => {
      for (let i = 0; i < 10; i++) await Promise.resolve()
    })

    expect(refreshCalls(fetchMock)).toBe(1)
    expect(container.textContent).toContain('Reabre Safent')
    expect(container.querySelector('nav')).toBeNull()

    const callsAfterSettling = fetchMock.mock.calls.length

    // Advance well past every polling interval in the app (fastest is 5s).
    await act(async () => {
      vi.advanceTimersByTime(60_000)
      for (let i = 0; i < 5; i++) await Promise.resolve()
    })

    expect(fetchMock.mock.calls.length).toBe(callsAfterSettling)
    expect(refreshCalls(fetchMock)).toBe(1)
  })
})
