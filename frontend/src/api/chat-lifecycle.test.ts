import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })

it('requests the actual cancellation endpoint with authentication and does not fabricate a terminal status', async () => {
  localStorage.setItem('safent_token', 'chat-qa-only')
  const { cancelTask } = await import('./client')
  const fetch = vi.fn().mockResolvedValue(Response.json({ ok: true, requested: true }))
  vi.stubGlobal('fetch', fetch)
  await expect(cancelTask('task/a')).resolves.toEqual({ ok: true, requested: true })
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/v1/tasks/task%2Fa/cancel', expect.objectContaining({
    method: 'POST', body: '{}', headers: expect.objectContaining({ Authorization: 'Bearer chat-qa-only' }),
  }))
})

it('preserves the actual structured 409 response from enqueue', async () => {
  localStorage.setItem('safent_token', 'chat-qa-only')
  const { postChat } = await import('./client')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: { code: 'conflict', message: 'Conflict' } }, { status: 409 })))
  await expect(postChat({ user_message: 'QA' })).rejects.toMatchObject({ code: 'conflict', status: 409 })
})

it('updates exactly the existing provider with PATCH instead of adding a duplicate', async () => {
  localStorage.setItem('safent_token', 'chat-qa-only')
  const { updateProvider } = await import('./client')
  const fetch = vi.fn().mockResolvedValue(Response.json({ id: 'configured-id', is_active: false }))
  vi.stubGlobal('fetch', fetch)
  await updateProvider('provider/a', { api_key: 'synthetic-replacement' })
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/v1/providers/provider%2Fa', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ api_key: 'synthetic-replacement' }) }))
})

it.each(['pending', 'in_progress', 'pending_approval', 'completed', 'failed', 'cancelled', 'rejected'])('reads exact owner task status %s without retaining provider errors', async status => {
  localStorage.setItem('safent_token', 'chat-qa-only')
  const { getChatTaskStatus } = await import('./client')
  const fetch = vi.fn().mockResolvedValue(Response.json({ task_id: 'task/a', status, attempts: 2, error: 'private-provider-detail' }))
  vi.stubGlobal('fetch', fetch)
  expect(await getChatTaskStatus('task/a')).toEqual({ task_id: 'task/a', status, attempts: 2 })
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/v1/tasks/task%2Fa/status', expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer chat-qa-only' }) }))
  expect(fetch.mock.calls[0][1].method).toBeUndefined()
})

it.each([null, {}, { task_id: 'other', status: 'failed', attempts: 2 }, { task_id: 'task-a', status: 'unknown', attempts: 2 }, { task_id: 'task-a', status: 'failed', attempts: -1 }, { task_id: 'task-a', status: 'failed', attempts: 0.5 }])('rejects invalid or differently scoped task status %#', async payload => {
  localStorage.setItem('safent_token', 'chat-qa-only')
  const { getChatTaskStatus } = await import('./client')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(payload)))
  await expect(getChatTaskStatus('task-a')).rejects.toMatchObject({ status: 502, body: null })
})

it.each([403, 404, 503])('keeps task lookup HTTP %s failures explicit and safe', async status => {
  localStorage.setItem('safent_token', 'chat-qa-only')
  const { getChatTaskStatus } = await import('./client')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: 'private-provider-detail' }, { status })))
  const error = await getChatTaskStatus('task-a').catch(failure => failure)
  expect(error).toMatchObject({ status, body: null })
  expect(error.message).not.toContain('private-provider-detail')
})

it.each(['completed', 'failed', 'cancelled', 'rejected'] as const)('forwards nested SSE DONE outcome %s, rejecting wrong task frames', async outcome => {
  const { openTaskStream } = await import('./client')
  let stream!: { onmessage: ((event: MessageEvent) => void) | null; close: ReturnType<typeof vi.fn> }
  vi.stubGlobal('EventSource', class {
    onmessage = null; onerror = null; close = vi.fn()
    constructor() { stream = this }
  })
  const callbacks = { onDelta: vi.fn(), onThinking: vi.fn(), onToolCall: vi.fn(), onStatus: vi.fn(), onDone: vi.fn(), onError: vi.fn() }
  openTaskStream('task-a', callbacks)
  stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ task_id: 'other', kind: 'done', outcome }) }))
  expect(callbacks.onDone).not.toHaveBeenCalled()
  stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ task_id: 'task-a', kind: 'done', payload: { outcome } }) }))
  expect(callbacks.onDone).toHaveBeenCalledExactlyOnceWith(outcome)
  expect(stream.close).toHaveBeenCalledOnce()
})

it('keeps EventSource errors nonterminal, suppresses duplicate frames and forwards the final outcome', async () => {
  const { openTaskStream } = await import('./client')
  let stream!: FakeEventSource
  class FakeEventSource {
    onmessage: ((event: MessageEvent) => void) | null = null
    onerror: (() => void) | null = null
    close = vi.fn()
    constructor() { stream = this }
  }
  vi.stubGlobal('EventSource', FakeEventSource)
  const callbacks = { onDelta: vi.fn(), onThinking: vi.fn(), onToolCall: vi.fn(), onStatus: vi.fn(), onDone: vi.fn(), onError: vi.fn() }
  openTaskStream('task-a', callbacks)
  stream.onerror?.()
  expect(callbacks.onStatus).toHaveBeenCalledWith('Reconectando con el agente…')
  expect(stream.close).not.toHaveBeenCalled()
  const frame = new MessageEvent('message', { data: JSON.stringify({ seq: 1, kind: 'delta', delta: 'One' }) })
  stream.onmessage?.(frame); stream.onmessage?.(frame)
  expect(callbacks.onDelta).toHaveBeenCalledExactlyOnceWith('One')
  stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ seq: 2, kind: 'error', error: 'stream_no_longer_available' }) }))
  expect(stream.close).not.toHaveBeenCalled()
  expect(callbacks.onError).toHaveBeenCalledExactlyOnceWith('stream_no_longer_available')
  expect(callbacks.onDone).not.toHaveBeenCalled()
  stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ seq: 3, kind: 'status', status: 'pending' }) }))
  expect(callbacks.onStatus).toHaveBeenLastCalledWith('pending')
  stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ seq: 4, kind: 'done', outcome: 'failed' }) }))
  expect(stream.close).toHaveBeenCalledOnce()
  expect(callbacks.onDone).toHaveBeenCalledExactlyOnceWith('failed')
  stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ seq: 5, kind: 'delta', delta: 'late' }) }))
  expect(callbacks.onDelta).toHaveBeenCalledOnce()
})
