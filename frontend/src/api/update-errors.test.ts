import { afterEach, expect, it, vi } from 'vitest'
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })
it.each(['getSystemUpdate', 'getInstallRequests'] as const)('%s preserves failure instead of inventing an empty/up-to-date state', async name => {
  localStorage.setItem('safent_token', 'update-fixture-only')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: 'unavailable' }, { status: 503 })))
  const client = await import('./client')
  await expect(client[name]()).rejects.toMatchObject({ status: 503 })
})
