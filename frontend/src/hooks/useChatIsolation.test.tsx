import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { getConversation, openTaskStream, postChat, type StreamCallbacks } from '../api/client'
import { useChat } from './useChat'

vi.mock('../api/client', () => ({
  postChat: vi.fn(), getConversation: vi.fn(), getRuntimeStatus: vi.fn().mockResolvedValue({ activity: [] }),
  openTaskStream: vi.fn().mockReturnValue({ close: vi.fn() }),
}))

let current: ReturnType<typeof useChat>
let host: HTMLDivElement
let root: Root
function Harness() { current = useChat(); return null }
async function mount() { await act(async () => { root.render(<Harness />) }) }
function deferred<T>() {
  let resolve!: (result: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); sessionStorage.clear() })

it('ignores a late send response after starting a new conversation', async () => {
  const pending = deferred<Awaited<ReturnType<typeof postChat>>>()
  vi.mocked(postChat).mockReturnValueOnce(pending.promise)
  await mount()
  let sent!: Promise<void>
  await act(async () => { sent = current.sendMessage('A') })
  await act(async () => { current.startNew() })
  await act(async () => { pending.resolve({ task_id: 'task-a' }); await sent })
  expect(current.convId).toBeNull()
  expect(current.messages).toEqual([])
  expect(current.status.phase).toBe('idle')
  expect(openTaskStream).not.toHaveBeenCalled()
  expect(sessionStorage.getItem('safent:taskId')).toBeNull()
})

it('ignores a late failed send without marking a different active thread as failed', async () => {
  const pending = deferred<Awaited<ReturnType<typeof postChat>>>()
  vi.mocked(postChat).mockReturnValueOnce(pending.promise).mockResolvedValueOnce({ task_id: 'task-b' })
  await mount()
  let sent!: Promise<void>
  await act(async () => { sent = current.sendMessage('A') })
  await act(async () => { current.startNew() })
  await act(async () => { await current.sendMessage('B') })
  await act(async () => { pending.reject(new Error('Late error A')); await sent })
  expect(current.status.phase).toBe('streaming')
  expect(current.messages[0]).toMatchObject({ type: 'user', text: 'B' })
  expect(sessionStorage.getItem('safent:taskId')).toBe('task-b')
})

it('rejects all obsolete stream callbacks, including done/error which could clear the new task', async () => {
  vi.mocked(postChat).mockResolvedValueOnce({ task_id: 'task-a' }).mockResolvedValueOnce({ task_id: 'task-b' })
  await mount()
  await act(async () => { await current.sendMessage('A') })
  const old = vi.mocked(openTaskStream).mock.calls[0]![1] as StreamCallbacks
  await act(async () => { current.startNew() })
  await act(async () => { await current.sendMessage('B') })
  await act(async () => {
    old.onDelta('Late A text'); old.onThinking('Late thought'); old.onStatus('Late A status')
    old.onError('Late A error'); old.onDone()
  })
  expect(current.status.phase).toBe('streaming')
  expect(sessionStorage.getItem('safent:taskId')).toBe('task-b')
  expect(JSON.stringify(current.messages)).not.toContain('Late')
})

it('keeps the most recently requested historical conversation when responses arrive out of order', async () => {
  const pending = deferred<Awaited<ReturnType<typeof getConversation>>>()
  vi.mocked(getConversation).mockReturnValueOnce(pending.promise).mockResolvedValueOnce({ id: 'b', messages: [{ role: 'user', content: 'B' }] })
  await mount()
  let loading!: Promise<void>
  await act(async () => { loading = current.loadConversation('a') })
  await act(async () => { await current.loadConversation('b') })
  await act(async () => { pending.resolve({ id: 'a', messages: [{ role: 'user', content: 'A' }] }); await loading })
  expect(current.convId).toBe('b')
  expect(current.messages[0]).toMatchObject({ text: 'B' })
})

it('keeps manual stop effective even while the enqueue response is still pending', async () => {
  const pending = deferred<Awaited<ReturnType<typeof postChat>>>()
  vi.mocked(postChat).mockReturnValueOnce(pending.promise)
  await mount()
  let sent!: Promise<void>
  await act(async () => { sent = current.sendMessage('A') })
  await act(async () => { current.stopStream() })
  await act(async () => { pending.resolve({ task_id: 'task-a' }); await sent })
  expect(current.status.phase).toBe('idle')
  expect(openTaskStream).not.toHaveBeenCalled()
  expect(postChat).toHaveBeenCalledOnce()
})

it('does not let a failed mount restore clear the session of a newly started task', async () => {
  sessionStorage.setItem('safent:convId', 'old-conversation')
  const pending = deferred<Awaited<ReturnType<typeof getConversation>>>()
  vi.mocked(getConversation).mockReturnValueOnce(pending.promise)
  vi.mocked(postChat).mockResolvedValueOnce({ task_id: 'new-task' })
  await mount()
  await act(async () => { current.startNew() })
  await act(async () => { await current.sendMessage('New message') })
  const currentId = current.convId
  await act(async () => { pending.reject(new Error('Old restore failed')) })
  expect(current.convId).toBe(currentId)
  expect(sessionStorage.getItem('safent:convId')).toBe(currentId)
  expect(sessionStorage.getItem('safent:taskId')).toBe('new-task')
  expect(current.status.phase).toBe('streaming')
})

it('still restores the current conversation under React StrictMode effect replay', async () => {
  sessionStorage.setItem('safent:convId', 'saved')
  vi.mocked(getConversation).mockResolvedValue({ id: 'saved', messages: [{ role: 'user', content: 'Saved message' }] })
  await act(async () => { root.render(<StrictMode><Harness /></StrictMode>) })
  expect(current.convId).toBe('saved')
  expect(current.messages[0]).toMatchObject({ text: 'Saved message' })
})

it('uses the newly chosen agent when the conversation id is still null', async () => {
  vi.mocked(postChat).mockResolvedValueOnce({ task_id: 'agent-task' })
  await mount()
  await act(async () => { current.startNewWithAgent('agent-b') })
  await act(async () => { await current.sendMessage('For agent B') })
  expect(postChat).toHaveBeenCalledWith(expect.objectContaining({ agent_id: 'agent-b', user_message: 'For agent B' }))
})
