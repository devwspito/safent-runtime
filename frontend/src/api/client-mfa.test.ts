import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })

it.each(['mfa_required', 'invalid_totp', 'mfa_not_enrolled', 'invalid_owner_approval'])('does not refresh or replay an approval on %s', async code => {
  vi.resetModules()
  localStorage.setItem('safent_token', 'unit-test-session')
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code, message: 'Verifica el código' } }), { status: 401 }))
  vi.stubGlobal('fetch', fetch)
  const { resolveApproval } = await import('./client')
  await expect(resolveApproval('proposal', 'once')).rejects.toMatchObject({ code, status: 401 })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/approvals/proposal')
  const { getAuthStatus } = await import('../lib/token')
  expect(getAuthStatus()).toEqual({ kind: 'authenticated' })
})
