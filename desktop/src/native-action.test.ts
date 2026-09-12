import { describe, expect, it, vi } from 'vitest'
import { nativeAction, nativeCancellation } from './native-action.js'

describe('native action feedback', () => {
  it('rejects double submission while the native command is pending', async () => {
    let done!: () => void
    const invoke = vi.fn(() => new Promise<void>(resolve => { done = resolve }))
    const run = nativeAction(invoke, vi.fn(), 'Unavailable')
    const first = run()
    expect(await run()).toBe(false)
    expect(invoke).toHaveBeenCalledTimes(1)
    done()
    expect(await first).toBe(true)
  })
  it('reports a failed command without leaking technical errors and permits retry', async () => {
    const invoke = vi.fn().mockRejectedValueOnce(new Error('secret internal details')).mockResolvedValueOnce(undefined)
    const show = vi.fn()
    const run = nativeAction(invoke, show, 'No se pudo solicitar')
    expect(await run()).toBe(false)
    expect(show).toHaveBeenLastCalledWith('No se pudo solicitar')
    expect(await run()).toBe(true)
    expect(show).toHaveBeenLastCalledWith('')
  })
})

describe('native cancellation feedback', () => {
  it('keeps the request pending after acknowledgement, without a second invocation', async () => {
    let finish!: () => void
    const invoke = vi.fn(() => new Promise<void>(yes => { finish = yes }))
    const action = nativeCancellation(invoke, vi.fn())
    action.sync(1, true)
    const pending = action.request()
    await action.request()
    expect(action.phase).toBe('requesting')
    expect(invoke).toHaveBeenCalledExactlyOnceWith(1)
    finish(); await pending
    expect(action.phase).toBe('requested')
    await action.request()
    expect(invoke).toHaveBeenCalledOnce()
    action.sync(1, false)
    expect(action.phase).toBe('idle')
  })
  it('ignores errors from a previous attempt and allows cancelling a new retry', async () => {
    let fail!: (reason: Error) => void
    const invoke = vi.fn().mockImplementationOnce(() => new Promise((_yes, no) => { fail = no })).mockResolvedValue(undefined)
    const changed = vi.fn()
    const action = nativeCancellation(invoke, changed)
    action.sync(1, true)
    const first = action.request()
    action.sync(2, true)
    await action.request()
    const count = changed.mock.calls.length
    fail(new Error('old secret')); await first
    expect(changed).toHaveBeenCalledTimes(count)
    expect(action.phase).toBe('requested')
    expect(invoke).toHaveBeenLastCalledWith(2)
  })
  it('reports recoverable failure but never invokes without an active attempt', async () => {
    const invoke = vi.fn().mockRejectedValueOnce(new Error('private')).mockResolvedValue(undefined)
    const action = nativeCancellation(invoke, vi.fn())
    await action.request()
    action.sync(1, false); await action.request()
    expect(invoke).not.toHaveBeenCalled()
    action.sync(1, true); await action.request()
    expect(action.phase).toBe('error')
    await action.request()
    expect(action.phase).toBe('requested')
  })
})
