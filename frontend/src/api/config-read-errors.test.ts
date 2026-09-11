import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })
it.each([
  ['native selection', async () => (await import('./client')).getNativeActive()],
  ['OAuth polling', async () => (await import('./client')).getProviderOAuthStatus('fake-session')],
  ['usage summary', async () => (await import('./client')).getUsageSummary('7d')],
  ['usage breakdown', async () => (await import('./client')).getUsageByAgent('7d')],
  ['usage chart', async () => (await import('./client')).getUsageTimeseries('7d', 'cost')],
  ['skill search', async () => (await import('./client')).searchSkillsHub('example')],
  ['skill hub', async () => (await import('./client')).listHubSkills()],
  ['skill progress', async () => (await import('./client')).getHubOpStatus('fake')],
  ['MCP search', async () => (await import('./client')).searchMcpRegistry('example')],
] as const)('%s preserves an HTTP failure instead of inventing absence', async (_label, read) => {
  localStorage.setItem('safent_token', 'fictitious-test-session')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"unavailable"}', { status:503 })))
  await expect(read()).rejects.toMatchObject({ status:503 })
})
it('keeps a confirmed empty native selection distinct from a failed read', async () => {
  localStorage.setItem('safent_token', 'fictitious-test-session')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', {status:200})))
  await expect((await import('./client')).getNativeActive()).resolves.toBeNull()
})
it('does not treat a malformed native selection as not configured',async()=>{
  localStorage.setItem('safent_token','fictitious-test-session')
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('{"error":"internal"}',{status:200})))
  await expect((await import('./client')).getNativeActive()).rejects.toMatchObject({status:502})
})
