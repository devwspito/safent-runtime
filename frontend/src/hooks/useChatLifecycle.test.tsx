import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cancelTask, getConversation, getRuntimeStatus, getChatTaskStatus, openTaskStream, postChat } from '../api/client'
import { useChat } from './useChat'

vi.mock('../api/client', () => ({
  postChat: vi.fn(), cancelTask: vi.fn(), getConversation: vi.fn(), getRuntimeStatus: vi.fn(),
  getChatTaskStatus: vi.fn(),
  openTaskStream: vi.fn(),
}))

let chat: ReturnType<typeof useChat>
let root: Root
let host: HTMLDivElement
const close = vi.fn()
function Harness() { chat = useChat(); return null }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const callbacks = () => {
  const calls = vi.mocked(openTaskStream).mock.calls
  return calls[calls.length - 1]![1]
}
async function start() {
  await act(async () => { root.render(<Harness />) })
  await act(async () => { await chat.sendMessage('One task') })
}
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); sessionStorage.clear()
  vi.mocked(postChat).mockResolvedValue({ task_id: 'task-a' })
  vi.mocked(openTaskStream).mockReturnValue({ close })
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [] })
  vi.mocked(getRuntimeStatus).mockResolvedValue({ state: 'idle', active_task_count: 0, activity: [] })
  vi.mocked(getChatTaskStatus).mockRejectedValue(new Error('legacy endpoint absent'))
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount()); host.remove(); sessionStorage.clear(); vi.useRealTimers()
})

it('requests cancellation once, preserves partial output and waits for terminal evidence', async () => {
  const pending = deferred<Awaited<ReturnType<typeof cancelTask>>>()
  vi.mocked(cancelTask).mockReturnValue(pending.promise)
  await start()
  await act(async () => { callbacks().onDelta('Partial'); chat.stopStream(); chat.stopStream() })
  expect(cancelTask).toHaveBeenCalledExactlyOnceWith('task-a')
  expect(chat.cancellation).toBe('requesting')
  expect(close).not.toHaveBeenCalled()
  await act(async () => { pending.resolve({ ok: true, requested: true }) })
  expect(chat.cancellation).toBe('requested')
  expect(chat.status.phase).toBe('streaming')
  expect(sessionStorage.getItem('safent:taskId')).toBe('task-a')
  await act(async () => { chat.stopStream(); callbacks().onDone() })
  expect(cancelTask).toHaveBeenCalledOnce()
  expect(chat.status.phase).toBe('idle')
  expect(chat.cancellation).toBe('idle')
  expect(JSON.stringify(chat.messages)).toContain('Partial')
  expect(postChat).toHaveBeenCalledOnce()
})

it('makes a cancellation failure retryable without detaching or auto-sending', async () => {
  vi.mocked(cancelTask).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ ok: true })
  await start()
  await act(async () => { chat.stopStream() })
  expect(chat.cancellation).toBe('error')
  expect(chat.status.phase).toBe('streaming')
  expect(close).not.toHaveBeenCalled()
  await act(async () => { chat.stopStream() })
  expect(chat.cancellation).toBe('requested')
  expect(cancelTask).toHaveBeenCalledTimes(2)
  expect(postChat).toHaveBeenCalledOnce()
})

it('discards a cancellation response after navigation and does not cancel the next task', async () => {
  const pending = deferred<Awaited<ReturnType<typeof cancelTask>>>()
  vi.mocked(cancelTask).mockReturnValueOnce(pending.promise)
  await start()
  await act(async () => { chat.stopStream(); chat.startNew() })
  vi.mocked(postChat).mockResolvedValueOnce({ task_id: 'task-b' })
  await act(async () => { await chat.sendMessage('B'); pending.reject(new Error('late A')) })
  expect(chat.cancellation).toBe('idle')
  expect(chat.status.phase).toBe('streaming')
  expect(sessionStorage.getItem('safent:taskId')).toBe('task-b')
  expect(cancelTask).toHaveBeenCalledExactlyOnceWith('task-a')
})

it('does not overwrite a completed task with a late cancellation acknowledgement', async () => {
  const pending = deferred<Awaited<ReturnType<typeof cancelTask>>>()
  vi.mocked(cancelTask).mockReturnValueOnce(pending.promise)
  await start()
  await act(async () => { chat.stopStream(); callbacks().onDone(); pending.resolve({ ok: true }) })
  expect(chat.cancellation).toBe('idle')
  expect(chat.status.phase).toBe('idle')
})

it('retains reconnection state across unrelated runtime activity until a real stream frame', async () => {
  vi.mocked(getRuntimeStatus).mockResolvedValue({ active_task_count: 1,
    activity: [{ task_id: 'other', tool: 'browser_navigate' }],
  } as Awaited<ReturnType<typeof getRuntimeStatus>>)
  await start()
  await act(async () => { callbacks().onStatus('Reconectando con el agente…'); await vi.advanceTimersByTimeAsync(2_000) })
  expect(chat.reconnecting).toBe(true)
  expect(chat.status).toEqual({ phase: 'streaming', statusText: 'Reconectando con el agente…' })
  await act(async () => { callbacks().onDelta('Back'); await vi.advanceTimersByTimeAsync(120) })
  expect(chat.reconnecting).toBe(false)
})

it('discloses a closed stream error, preserves its handle, and adopts only this task’s terminal mirror', async () => {
  await start()
  await act(async () => { callbacks().onDelta('Partial'); callbacks().onError('private upstream text') })
  expect(chat.streamError).toBe(true)
  expect(chat.status.phase).toBe('streaming')
  expect(sessionStorage.getItem('safent:taskId')).toBe('task-a')
  expect(JSON.stringify(chat)).not.toContain('private upstream')
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'other', status: 'complete', content: 'Wrong task' },
  ] })
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
  expect(chat.streamError).toBe(true)
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'task-a', status: 'complete', content: 'Final mirror' },
  ] })
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
  expect(chat.streamError).toBe(false)
  expect(chat.status.phase).toBe('idle')
  expect(JSON.stringify(chat.messages)).toContain('Final mirror')
  expect(JSON.stringify(chat.messages)).not.toContain('Wrong task')
  expect(postChat).toHaveBeenCalledOnce()
})

it('does not revive a finished mirror answer from a previously queued streaming batch', async () => {
  await start()
  await act(async () => { await vi.advanceTimersByTimeAsync(1_950); callbacks().onStatus('Working') })
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'task-a', status: 'complete', content: 'Final' },
  ] })
  await act(async () => { await vi.advanceTimersByTimeAsync(500) })
  expect(chat.status.phase).toBe('idle')
})

it('does not treat missing or unknown mirror status as terminal evidence', async () => {
  await start()
  for (const status of [undefined, 'unknown']) {
    vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
      { role: 'assistant', task_id: 'task-a', status, content: 'Unconfirmed partial' },
    ] })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(chat.status.phase).toBe('streaming')
    expect(sessionStorage.getItem('safent:taskId')).toBe('task-a')
  }
})

it('restores an unknown-status saved task as pending, not completed', async () => {
  sessionStorage.setItem('safent:convId', 'saved')
  sessionStorage.setItem('safent:taskId', 'saved-task')
  vi.mocked(getConversation).mockResolvedValue({ id: 'saved', messages: [
    { role: 'assistant', task_id: 'saved-task', content: 'Partial saved text' },
  ] })
  await act(async () => { root.render(<Harness />) })
  expect(openTaskStream).toHaveBeenCalledWith('saved-task', expect.any(Object))
  expect(chat.messages).toEqual([expect.objectContaining({ type: 'assistant', isStreaming: true })])
  expect(sessionStorage.getItem('safent:taskId')).toBe('saved-task')
})

it('preserves a structured enqueue error code without assigning a meaning to HTTP 409', async () => {
  vi.mocked(postChat).mockRejectedValueOnce(Object.assign(new Error('HTTP 409'), { code: 'different_conflict' }))
  await start()
  expect(chat.status).toEqual({ phase: 'error', message: 'HTTP 409', code: 'different_conflict' })
  expect(openTaskStream).not.toHaveBeenCalled()
})

it.each(['failed', 'cancelled'] as const)('settles an interrupted stream from precise task status %s even without a mirror', async status => {
  await start()
  vi.mocked(getChatTaskStatus).mockResolvedValue({ task_id: 'task-a', status, attempts: 3 })
  await act(async () => { callbacks().onDelta('Partial output'); callbacks().onError('private error') })
  expect(chat.status.phase).toBe(status === 'failed' ? 'error' : 'idle')
  expect(chat.streamError).toBe(false)
  expect(chat.reconnecting).toBe(false)
  expect(chat.cancellation).toBe('idle')
  expect(chat.messages[1]).toMatchObject({ type: 'assistant', isStreaming: false, activityText: '', thinkingDone: true })
  expect(JSON.stringify(chat.messages)).toContain('Partial output')
  expect(JSON.stringify(chat)).not.toContain('private error')
  expect(sessionStorage.getItem('safent:taskId')).toBeNull()
  const polls = vi.mocked(getChatTaskStatus).mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); window.dispatchEvent(new Event('focus')) })
  expect(getChatTaskStatus).toHaveBeenCalledTimes(polls)
  expect(postChat).toHaveBeenCalledOnce()
})

it.each(['failed', 'cancelled'] as const)('adopts exactly one %s terminal mirror row without a second warning', async status => {
  await start()
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'task-a', status: 'streaming', content: 'Old partial' },
    { role: 'assistant', task_id: 'task-a', status, content: 'Terminal narrative once' },
  ] })
  await act(async () => { callbacks().onError('transport'); await vi.advanceTimersByTimeAsync(2_000) })
  expect(chat.messages.filter(message => message.type === 'assistant')).toHaveLength(1)
  expect(JSON.stringify(chat.messages).match(/Terminal narrative once/g)).toHaveLength(1)
  expect(chat.status).toEqual({ phase: 'idle' })
  expect(chat.streamError).toBe(false)
})

it.each(['failed', 'cancelled'] as const)('DONE %s releases busy immediately, enriches only its own bubble, and ignores late frames', async outcome => {
  const mirror = deferred<Awaited<ReturnType<typeof getConversation>>>()
  await start()
  vi.mocked(getConversation).mockReturnValue(mirror.promise)
  const stream = callbacks()
  await act(async () => { stream.onDelta('Partial'); stream.onDone(outcome) })
  expect(chat.status.phase).toBe(outcome === 'failed' ? 'error' : 'idle')
  expect(chat.messages[1]).toMatchObject({ isStreaming: false })
  expect(sessionStorage.getItem('safent:taskId')).toBeNull()
  await act(async () => {
    stream.onStatus('late'); stream.onError('late'); stream.onDelta('Late text')
    mirror.resolve({ id: 'conversation', messages: [{ role: 'assistant', task_id: 'task-a', status: outcome, content: 'One final narrative' }] })
  })
  expect(chat.status).toEqual({ phase: 'idle' })
  expect(JSON.stringify(chat.messages)).toContain('One final narrative')
  expect(JSON.stringify(chat.messages)).not.toContain('Late text')
  expect(chat.messages).toHaveLength(2)
  expect(chat.streamError).toBe(false)
  expect(postChat).toHaveBeenCalledOnce()
})

it('keeps backend retries active instead of accepting an old complete mirror as final', async () => {
  vi.mocked(getChatTaskStatus).mockResolvedValue({ task_id: 'task-a', status: 'pending', attempts: 1 })
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'task-a', status: 'complete', content: 'Obsolete first-attempt error' },
  ] })
  await start()
  await act(async () => { callbacks().onError('attempt failed'); callbacks().onStatus('pending'); await vi.advanceTimersByTimeAsync(2_000) })
  expect(chat.status.phase).toBe('streaming')
  expect(chat.streamError).toBe(false)
  expect(JSON.stringify(chat.messages)).not.toContain('Obsolete first-attempt error')
  expect(close).not.toHaveBeenCalled()
  vi.mocked(getChatTaskStatus).mockResolvedValue({ task_id: 'task-a', status: 'failed', attempts: 3 })
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'task-a', status: 'failed', content: 'Final attempt only' },
  ] })
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  expect(chat.status.phase).toBe('idle')
  expect(JSON.stringify(chat.messages)).toContain('Final attempt only')
  expect(postChat).toHaveBeenCalledOnce()
})

it('recovers on focus, keeps polling single-flight, and prevents a queued batch reviving terminal state', async () => {
  const pending = deferred<Awaited<ReturnType<typeof getChatTaskStatus>>>()
  vi.mocked(getChatTaskStatus).mockReturnValue(pending.promise)
  await start()
  await act(async () => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(4_000) })
  expect(getChatTaskStatus).toHaveBeenCalledExactlyOnceWith('task-a')
  await act(async () => { callbacks().onStatus('Queued status'); pending.resolve({ task_id: 'task-a', status: 'cancelled', attempts: 1 }) })
  await act(async () => { await vi.advanceTimersByTimeAsync(500) })
  expect(chat.status).toEqual({ phase: 'idle', outcome: 'cancelled' })
  expect(chat.messages[1]).toMatchObject({ isStreaming: false })
})

it('releases a failed task without waiting for slow activity or mirror reads, then enriches one bubble', async () => {
  const mirror = deferred<Awaited<ReturnType<typeof getConversation>>>()
  const runtime = deferred<Awaited<ReturnType<typeof getRuntimeStatus>>>()
  await start()
  vi.mocked(getConversation).mockReturnValue(mirror.promise)
  vi.mocked(getRuntimeStatus).mockReturnValue(runtime.promise)
  vi.mocked(getChatTaskStatus).mockResolvedValueOnce({ task_id: 'task-a', status: 'pending', attempts: 1 })
    .mockResolvedValue({ task_id: 'task-a', status: 'failed', attempts: 3 })
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  expect(chat.status.phase).toBe('streaming')
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
  expect(getChatTaskStatus).toHaveBeenCalledTimes(2)
  expect(getConversation).toHaveBeenCalledOnce()
  expect(getRuntimeStatus).toHaveBeenCalledOnce()
  expect(chat.status.phase).toBe('error')
  expect(chat.messages[1]).toMatchObject({ isStreaming: false })
  expect(sessionStorage.getItem('safent:taskId')).toBeNull()
  await act(async () => {
    mirror.resolve({ id: 'conversation', messages: [{ role: 'assistant', task_id: 'task-a', status: 'failed', content: 'Canonical terminal narrative' }] })
    runtime.resolve({ active_task_count: 1, activity: [{ task_id: 'task-a', tool: 'browser_navigate' }] } as Awaited<ReturnType<typeof getRuntimeStatus>>)
  })
  expect(chat.status).toEqual({ phase: 'idle' })
  expect(chat.messages).toHaveLength(2)
  expect(JSON.stringify(chat.messages).match(/Canonical terminal narrative/g)).toHaveLength(1)
  expect(postChat).toHaveBeenCalledOnce()
})

it('does not adopt an obsolete mirror if task status becomes unavailable during a confirmed retry', async () => {
  await start()
  vi.mocked(getChatTaskStatus).mockResolvedValueOnce({ task_id: 'task-a', status: 'pending', attempts: 1 })
    .mockRejectedValue(new Error('temporarily unavailable'))
  vi.mocked(getConversation).mockResolvedValue({ id: 'conversation', messages: [
    { role: 'assistant', task_id: 'task-a', status: 'complete', content: 'Obsolete first-attempt error' },
  ] })
  await act(async () => { window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(2_000) })
  expect(chat.status.phase).toBe('streaming')
  expect(JSON.stringify(chat.messages)).not.toContain('Obsolete first-attempt error')
  expect(close).not.toHaveBeenCalled()
})

it('does not mistake a missing, failed or differently scoped task lookup for completion', async () => {
  await start()
  for (const response of [() => Promise.reject(new Error('404')), () => Promise.reject(new Error('503')), () => Promise.resolve({ task_id: 'other', status: 'failed', attempts: 3 } as const)]) {
    vi.mocked(getChatTaskStatus).mockImplementationOnce(response)
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    expect(chat.status.phase).toBe('streaming')
    expect(sessionStorage.getItem('safent:taskId')).toBe('task-a')
  }
  expect(postChat).toHaveBeenCalledOnce()
})

it.each(['failed', 'cancelled'] as const)('restores a saved %s mirror without reopening the stream or keeping its task handle', async status => {
  sessionStorage.setItem('safent:convId', 'saved'); sessionStorage.setItem('safent:taskId', 'saved-task')
  vi.mocked(getConversation).mockResolvedValue({ id: 'saved', messages: [{ role: 'assistant', task_id: 'saved-task', status, content: 'Terminal saved narrative' }] })
  await act(async () => { root.render(<Harness />) })
  expect(openTaskStream).not.toHaveBeenCalled()
  expect(chat.status.phase).toBe('idle')
  expect(chat.messages).toEqual([expect.objectContaining({ isStreaming: false })])
  expect(sessionStorage.getItem('safent:taskId')).toBeNull()
})

it('does not let a late terminal status or mirror affect another task after navigation', async () => {
  const pending = deferred<Awaited<ReturnType<typeof getChatTaskStatus>>>()
  await start(); vi.mocked(getChatTaskStatus).mockReturnValue(pending.promise)
  await act(async () => { window.dispatchEvent(new Event('focus')); chat.startNew() })
  vi.mocked(postChat).mockResolvedValue({ task_id: 'task-b' })
  await act(async () => { await chat.sendMessage('Second'); pending.resolve({ task_id: 'task-a', status: 'failed', attempts: 3 }) })
  expect(chat.status.phase).toBe('streaming')
  expect(sessionStorage.getItem('safent:taskId')).toBe('task-b')
  expect(JSON.stringify(chat.messages)).not.toContain('Task failed')
})
