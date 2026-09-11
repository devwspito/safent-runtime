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

it('separates automatic EventSource reconnection from a closed error frame and suppresses duplicate frames', async () => {
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
  expect(stream.close).toHaveBeenCalledOnce()
  expect(callbacks.onError).toHaveBeenCalledExactlyOnceWith('stream_no_longer_available')
  expect(callbacks.onDone).not.toHaveBeenCalled()
})
