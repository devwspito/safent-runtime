import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useContextSource } from './useContextSource'
let host: HTMLDivElement
let root: Root
let source: ReturnType<typeof useContextSource<string>>
const valid = (row: string) => typeof row === 'string'
const load = vi.fn<() => Promise<string[]>>()
function Harness() { source = useContextSource(load, valid); return <>{source.data?.join(',')}</> }
function pending() { let resolve!: (data: string[]) => void; let reject!: (error: Error) => void; const promise = new Promise<string[]>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
beforeEach(() => { vi.resetAllMocks(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true); host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })

it('keeps latest data when an older request fails after a newer refresh', async () => {
  const old = pending(); const fresh = pending()
  load.mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
  await act(async () => { root.render(<Harness />) })
  await act(async () => { void source.refresh() })
  await act(async () => { fresh.resolve(['new']) })
  await act(async () => { old.reject(new Error('old failure')) })
  expect(source.data).toEqual(['new']); expect(source.error).toBe(false); expect(source.loading).toBe(false)
})

it('never overlaps polling and rejects invalid data rather than treating it as empty', async () => {
  const slow = pending(); load.mockReturnValueOnce(slow.promise)
  await act(async () => { root.render(<Harness />) })
  await act(async () => { await source.refresh(true); await source.refresh(true) })
  expect(load).toHaveBeenCalledTimes(1)
  await act(async () => { slow.resolve({ invalid: true } as unknown as string[]) })
  expect(source.data).toBeNull(); expect(source.error).toBe(true)
})

it('does not publish a response from an old conversation after keyed remount', async () => {
  const old = pending(); load.mockReturnValueOnce(old.promise).mockResolvedValueOnce(['thread-b'])
  await act(async () => { root.render(<Harness key="thread-a" />) })
  await act(async () => { root.render(<Harness key="thread-b" />) })
  await act(async () => { old.resolve(['thread-a']) })
  expect(host.textContent).toBe('thread-b')
})

it('survives StrictMode cleanup and ignores superseded effect response', async () => {
  const old = pending(); load.mockReturnValueOnce(old.promise).mockResolvedValueOnce(['active'])
  await act(async () => { root.render(<StrictMode><Harness /></StrictMode>) })
  await act(async () => { old.resolve(['stale']) })
  expect(source.data).toEqual(['active'])
})
