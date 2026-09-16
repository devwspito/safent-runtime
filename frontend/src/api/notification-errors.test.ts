import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })
it.each(['listNotifications', 'getUnreadCount'] as const)('%s preserves an HTTP failure rather than an empty inbox', async (method) => {
  localStorage.setItem('safent_token', 'notification-fixture-only')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"unavailable"}', { status: 503 })))
  const client = await import('./client')
  await expect(client[method]()).rejects.toMatchObject({ status: 503 })
})
