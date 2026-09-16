import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })

it('transports an exact-action grant only in its header and never retries a rejected grant', async () => {
  vi.resetModules()
  localStorage.setItem('safent_token', 'test-owner-session')
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: { code: 'invalid_owner_approval', message: 'Revisa y confirma de nuevo.' },
  }), { status: 401 }))
  vi.stubGlobal('fetch', fetch)
  const { installSkill } = await import('./client')
  await expect(installSkill('skill-a', true, 'one-use-grant')).rejects.toMatchObject({
    code: 'invalid_owner_approval', status: 401,
  })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/skills/hub/install')
  const options = fetch.mock.calls[0][1]
  expect(options.headers).toMatchObject({
    Authorization: 'Bearer test-owner-session', 'X-Owner-Approval-Grant': 'one-use-grant',
  })
  expect(JSON.parse(options.body)).toEqual({ identifier: 'skill-a', force: true })
  const { getAuthStatus } = await import('../lib/token')
  expect(getAuthStatus()).toEqual({ kind: 'authenticated' })
})

it.each(['add', 'managed'])('passes an MCP %s grant without replaying a rejected operation', async (kind) => {
  localStorage.setItem('safent_token', 'test-owner-session')
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: { code: 'invalid_owner_approval' },
  }), { status: 401 }))
  vi.stubGlobal('fetch', fetch)
  const { addMcpServer, connectManagedRemote } = await import('./client')
  const operation = kind === 'add'
    ? addMcpServer({ server_id: 'example', force: true }, 'mcp-grant')
    : connectManagedRemote('safent-ads', 'https://ads.example.com/mcp', true, 'mcp-grant')
  await expect(operation).rejects.toMatchObject({ code: 'invalid_owner_approval' })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][1].headers['X-Owner-Approval-Grant']).toBe('mcp-grant')
  expect(fetch.mock.calls[0][1].body).not.toContain('mcp-grant')
})
