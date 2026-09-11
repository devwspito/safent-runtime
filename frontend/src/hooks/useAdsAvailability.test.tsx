import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as InboundDelegationCard.test.tsx (no
// @testing-library / react-hooks in this project) — mount a tiny harness
// component that calls the hook and renders its state as text.

const { mintAdsBridgeSession } = vi.hoisted(() => ({
  mintAdsBridgeSession: vi.fn(),
}))

vi.mock('../api/client', () => ({ mintAdsBridgeSession }))

import { useAdsAvailability } from './useAdsAvailability'

function Harness({ pollMs }: { pollMs?: number }) {
  const av = useAdsAvailability(pollMs)
  return React.createElement(
    'div',
    { 'data-status': av.status, 'data-reason': av.reason ?? '' },
    React.createElement('button', { onClick: av.refresh }, 'refresh'),
  )
}

describe('useAdsAvailability', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    mintAdsBridgeSession.mockReset()
    vi.useFakeTimers()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
    vi.useRealTimers()
  })

  it('starts in "loading" before the first response arrives', () => {
    mintAdsBridgeSession.mockReturnValue(new Promise(() => { /* never resolves */ }))

    act(() => {
      root.render(React.createElement(Harness))
    })

    const div = container.querySelector('div')
    expect(div?.getAttribute('data-status')).toBe('loading')
  })

  it('reports "ready" once the first response resolves', async () => {
    mintAdsBridgeSession.mockResolvedValue({ status: 'ready', reason: null })

    act(() => {
      root.render(React.createElement(Harness))
    })
    await act(async () => { await Promise.resolve() })

    const div = container.querySelector('div')
    expect(div?.getAttribute('data-status')).toBe('ready')
    expect(div?.getAttribute('data-reason')).toBe('')
  })

  it('reports "unavailable" with the reason from the response', async () => {
    mintAdsBridgeSession.mockResolvedValue({ status: 'unavailable', reason: 'no_accounts' })

    act(() => {
      root.render(React.createElement(Harness))
    })
    await act(async () => { await Promise.resolve() })

    const div = container.querySelector('div')
    expect(div?.getAttribute('data-status')).toBe('unavailable')
    expect(div?.getAttribute('data-reason')).toBe('no_accounts')
  })

  it('polls again after pollMs elapses', async () => {
    mintAdsBridgeSession
      .mockResolvedValueOnce({ status: 'unavailable', reason: 'unreachable' })
      .mockResolvedValueOnce({ status: 'ready', reason: null })

    act(() => {
      root.render(React.createElement(Harness, { pollMs: 1000 }))
    })
    await act(async () => { await Promise.resolve() })
    expect(container.querySelector('div')?.getAttribute('data-status')).toBe('unavailable')

    await act(async () => {
      vi.advanceTimersByTime(1000)
      await Promise.resolve()
    })

    expect(container.querySelector('div')?.getAttribute('data-status')).toBe('ready')
    expect(mintAdsBridgeSession).toHaveBeenCalledTimes(2)
  })

  it('refresh() re-checks immediately, outside the poll interval', async () => {
    mintAdsBridgeSession
      .mockResolvedValueOnce({ status: 'unavailable', reason: 'unreachable' })
      .mockResolvedValueOnce({ status: 'ready', reason: null })

    act(() => {
      root.render(React.createElement(Harness, { pollMs: 60_000 }))
    })
    await act(async () => { await Promise.resolve() })
    expect(container.querySelector('div')?.getAttribute('data-status')).toBe('unavailable')

    const button = container.querySelector('button')!
    await act(async () => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(container.querySelector('div')?.getAttribute('data-status')).toBe('ready')
    expect(mintAdsBridgeSession).toHaveBeenCalledTimes(2)
  })

  it('does not update state after unmount (no act-outside-test warning / leak)', async () => {
    let resolvePromise: (value: { status: 'ready'; reason: null }) => void
    mintAdsBridgeSession.mockReturnValue(
      new Promise((resolve) => { resolvePromise = resolve }),
    )

    act(() => {
      root.render(React.createElement(Harness))
    })
    act(() => { root.unmount() })

    // Resolving after unmount must not throw / warn — the hook guards with
    // an alive ref.
    await act(async () => {
      resolvePromise({ status: 'ready', reason: null })
      await Promise.resolve()
    })
  })

  it('does not overlap polling and manual refresh while an availability check is pending', async () => {
    let finish!: (value: { status: 'unavailable'; reason: 'unauthorized' }) => void
    mintAdsBridgeSession.mockReturnValue(new Promise(resolve => { finish = resolve }))
    act(() => root.render(React.createElement(Harness, { pollMs: 1000 })))
    await act(async () => {
      container.querySelector('button')!.click()
      vi.advanceTimersByTime(5000)
    })
    expect(mintAdsBridgeSession).toHaveBeenCalledTimes(1)
    await act(async () => finish({ status: 'unavailable', reason: 'unauthorized' }))
    expect(container.querySelector('div')?.dataset.reason).toBe('unauthorized')
  })

  it('ignores a previous effect response after polling configuration changes', async () => {
    let old!: (value: { status: 'ready'; reason: null }) => void
    mintAdsBridgeSession.mockReturnValueOnce(new Promise(resolve => { old = resolve }))
      .mockResolvedValueOnce({ status: 'unavailable', reason: 'unauthorized' })
    act(() => root.render(React.createElement(Harness, { pollMs: 1000 })))
    await act(async () => root.render(React.createElement(Harness, { pollMs: 2000 })))
    await act(async () => old({ status: 'ready', reason: null }))
    expect(container.querySelector('div')?.dataset.reason).toBe('unauthorized')
  })

  it('handles a rejected check as unavailable rather than leaving loading forever', async () => {
    mintAdsBridgeSession.mockRejectedValue(new Error('offline'))
    await act(async () => root.render(React.createElement(Harness)))
    expect(container.querySelector('div')?.dataset.reason).toBe('unreachable')
  })
})
