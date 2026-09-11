import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cancelTask, getConversation, getRuntimeStatus, openTaskStream, postChat } from '../api/client'
import { useChat } from './useChat'

vi.mock('../api/client', () => ({
  postChat: vi.fn(), cancelTask: vi.fn(), getConversation: vi.fn(), getRuntimeStatus: vi.fn(),
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
