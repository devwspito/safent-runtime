import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })

it('does not fabricate a balanced policy when the server cannot read owner restrictions', async () => {
  localStorage.setItem('safent_token', 'test-owner-session')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: { error: 'policy_unavailable' },
  }), { status: 503 })))
  const { getPolicies } = await import('./client')
  await expect(getPolicies()).rejects.toMatchObject({ status: 503 })
})
