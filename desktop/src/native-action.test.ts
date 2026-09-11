import { describe, expect, it, vi } from 'vitest'
import { nativeAction } from './native-action.js'

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
