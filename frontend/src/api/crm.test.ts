import { afterEach, expect, it, vi } from 'vitest'
vi.mock('../lib/token', () => ({
  token: () => 'owner-fixture',
  getAuthStatus: () => 'authenticated',
}))
import { listCrmConnections, readCrm } from './crm'

const context = 'b'.repeat(64)
const connection = {
  id: 'known',
  name: 'CRM',
  read_only: true as const,
  operations: [{ method: 'GET' as const, path: '/contacts' }],
}
afterEach(() => vi.unstubAllGlobals())

it('uses authenticated no-store transport and sends exact pairing/operation', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response(
        JSON.stringify({
          context,
          data: [],
          operation: connection.operations[0],
          untrusted_external_data: true,
        }),
      ),
    )
  vi.stubGlobal('fetch', fetch)
  await readCrm(connection, '/contacts', context, new AbortController().signal)
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/crm/known/read',
    expect.objectContaining({
      method: 'POST',
      cache: 'no-store',
      body: JSON.stringify({ context, path: '/contacts' }),
      headers: expect.objectContaining({
        Authorization: 'Bearer owner-fixture',
      }),
    }),
  )
})

it('rejects unlisted operation before fetch', async () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  await expect(
    readCrm(connection, '/delete', context, new AbortController().signal),
  ).rejects.toMatchObject({ status: 403 })
  expect(fetch).not.toHaveBeenCalled()
})

it('does not retry a 401 read or turn unavailable into empty', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }))
  vi.stubGlobal('fetch', fetch)
  await expect(
    listCrmConnections(new AbortController().signal),
  ).rejects.toMatchObject({ status: 401 })
  expect(fetch).toHaveBeenCalledTimes(1)
})

it('rejects response from a different pairing even on HTTP200', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            context: 'c'.repeat(64),
            data: [],
            operation: connection.operations[0],
            untrusted_external_data: true,
          }),
        ),
      ),
  )
  await expect(
    readCrm(connection, '/contacts', context, new AbortController().signal),
  ).rejects.toMatchObject({ status: 502 })
})

it('checks cancellation after a delayed response body', async () => {
  let resolve!: (value: unknown) => void
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        new Promise((done) => {
          resolve = done
        }),
    }),
  )
  const controller = new AbortController()
  const pending = listCrmConnections(controller.signal)
  await vi.waitFor(() => expect(resolve).toBeDefined())
  controller.abort()
  resolve({ context, connections: [], limit: 100 })
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
})
