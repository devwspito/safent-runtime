import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// token.ts computes its module-level state (the bearer + the initial
// AuthStatus) at IMPORT time from window.__SAFENT_TOKEN__ / localStorage —
// exactly like a real page load. To exercise every starting condition we
// reset the module registry and re-import fresh per test.

async function freshToken() {
  vi.resetModules()
  return import('./token')
}

function setInjected(value: string | undefined) {
  const w = window as unknown as Record<string, unknown>
  if (value === undefined) delete w['__SAFENT_TOKEN__']
  else w['__SAFENT_TOKEN__'] = value
}

describe('token module state on load', () => {
  beforeEach(() => {
    localStorage.clear()
    setInjected(undefined)
  })

  it('is unauthenticated/no_token with no injected and no cached bearer', async () => {
    const { token, getAuthStatus } = await freshToken()
    expect(token()).toBe('')
    expect(getAuthStatus()).toEqual({ kind: 'unauthenticated', reason: 'no_token' })
  })

  it('is authenticated with an injected bearer, and persists it for later loads', async () => {
    setInjected('fresh-secret')
    const { token, getAuthStatus } = await freshToken()
    expect(token()).toBe('fresh-secret')
    expect(getAuthStatus()).toEqual({ kind: 'authenticated' })
    expect(localStorage.getItem('safent_token')).toBe('fresh-secret')
  })

  it('is authenticated with only a cached bearer (direct nav / reload, no ?k=)', async () => {
    localStorage.setItem('safent_token', 'cached-secret')
    const { token, getAuthStatus } = await freshToken()
    expect(token()).toBe('cached-secret')
    expect(getAuthStatus()).toEqual({ kind: 'authenticated' })
  })
})

describe('refreshToken', () => {
  beforeEach(() => {
    localStorage.clear()
    setInjected(undefined)
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('never calls fetch when there is no bearer to refresh', async () => {
    const { refreshToken } = await freshToken()
    const ok = await refreshToken()
    expect(ok).toBe(false)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('adopts the renewed bearer and stays authenticated on success', async () => {
    setInjected('stale-secret')
    const { refreshToken, token, getAuthStatus } = await freshToken()
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'renewed-secret' }),
    } as Response)

    const ok = await refreshToken()

    expect(ok).toBe(true)
    expect(token()).toBe('renewed-secret')
    expect(getAuthStatus()).toEqual({ kind: 'authenticated' })
    expect(localStorage.getItem('safent_token')).toBe('renewed-secret')
  })

  it('drops the bearer and flips to unauthenticated/refresh_failed on a non-ok response', async () => {
    setInjected('stale-secret')
    const { refreshToken, token, getAuthStatus } = await freshToken()
    vi.mocked(fetch).mockResolvedValue({ ok: false, json: async () => ({}) } as Response)

    const ok = await refreshToken()

    expect(ok).toBe(false)
    expect(token()).toBe('')
    expect(getAuthStatus()).toEqual({ kind: 'unauthenticated', reason: 'refresh_failed' })
    expect(localStorage.getItem('safent_token')).toBeNull()
  })

  it('flips to unauthenticated/refresh_failed on a network error too (one attempt, no loop)', async () => {
    setInjected('stale-secret')
    const { refreshToken, getAuthStatus } = await freshToken()
    vi.mocked(fetch).mockRejectedValue(new TypeError('network down'))

    const ok = await refreshToken()

    expect(ok).toBe(false)
    expect(getAuthStatus()).toEqual({ kind: 'unauthenticated', reason: 'refresh_failed' })
  })

  it('dedupes concurrent callers into exactly ONE network call (the request-storm fix)', async () => {
    setInjected('stale-secret')
    const { refreshToken } = await freshToken()
    let resolveFetch!: (r: Response) => void
    vi.mocked(fetch).mockReturnValue(new Promise((resolve) => { resolveFetch = resolve }))

    const first = refreshToken()
    const second = refreshToken()
    const third = refreshToken()

    resolveFetch({ ok: true, json: async () => ({ token: 'renewed' }) } as Response)
    const [a, b, c] = await Promise.all([first, second, third])

    expect(fetch).toHaveBeenCalledTimes(1)
    expect([a, b, c]).toEqual([true, true, true])
  })

  it('notifies subscribers exactly once per real transition', async () => {
    setInjected('stale-secret')
    const { refreshToken, subscribeAuthStatus } = await freshToken()
    vi.mocked(fetch).mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    const listener = vi.fn()
    subscribeAuthStatus(listener)

    await refreshToken()
    // Token is now empty — a second call short-circuits before fetch and
    // before touching the (already-set) status, so no extra notification.
    await refreshToken()

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('unsubscribe stops further notifications', async () => {
    setInjected('stale-secret')
    const { refreshToken, subscribeAuthStatus } = await freshToken()
    vi.mocked(fetch).mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    const listener = vi.fn()
    const unsubscribe = subscribeAuthStatus(listener)
    unsubscribe()

    await refreshToken()

    expect(listener).not.toHaveBeenCalled()
  })
})
