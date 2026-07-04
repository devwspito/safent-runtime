import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, listInboundDelegations, resolveInboundDelegation } from './client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('inbound delegations api client', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('GETs the pending list from the exact backend shape (no from_agent_id/correlation_id)', async () => {
    const rows = [
      {
        message_id: 'msg-1',
        from_employee_id: 'ana@empresa.com',
        body: 'Revisa el contrato adjunto y responde al cliente.',
        issued_at: '2026-07-01T10:00:00Z',
        created_at: '2026-07-01T10:00:00Z',
      },
    ]
    fetchMock.mockResolvedValue(jsonResponse(rows))

    const result = await listInboundDelegations()

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/inbound-delegations')
    expect(init.method).toBeUndefined()
    expect(result).toEqual(rows)
  })

  it('fails soft to [] when the daemon is unavailable', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'agent unavailable' }, 503))

    const result = await listInboundDelegations()

    expect(result).toEqual([])
  })

  it('POSTs the decision to resolve one delegation', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, task_id: 'task-1' }))

    const result = await resolveInboundDelegation('msg-1', 'approve')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/inbound-delegations/msg-1')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ decision: 'approve' }))
    expect(result).toEqual({ ok: true, task_id: 'task-1' })
  })

  it('URL-encodes the message id path segment', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))

    await resolveInboundDelegation('msg/with space', 'reject')

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`/api/v1/inbound-delegations/${encodeURIComponent('msg/with space')}`)
  })

  it('throws ApiError when the mutator reports ok:false', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: false, error: "decision inválida: 'x'" }))

    await expect(resolveInboundDelegation('msg-1', 'approve')).rejects.toBeInstanceOf(ApiError)
  })
})
