import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })

it('propagates a failed recent-conversations request instead of returning an empty history', async () => {
  vi.resetModules()
  localStorage.setItem('safent_token', 'test-owner-session')
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Unavailable' }), { status: 503 }))
  vi.stubGlobal('fetch', fetch)
  const { listConversations } = await import('./client')
  await expect(listConversations()).rejects.toMatchObject({ status: 503 })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/chat/conversations')
})
