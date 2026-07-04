import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (minimal deps, plain DOM assertions) rather than pulling in a new test
// dependency for a single component.

const { resolveInboundDelegation, sileoSuccess, sileoError } = vi.hoisted(() => ({
  resolveInboundDelegation: vi.fn(),
  sileoSuccess: vi.fn(),
  sileoError: vi.fn(),
}))

vi.mock('../api/client', () => ({ resolveInboundDelegation }))
vi.mock('sileo', () => ({ sileo: { success: sileoSuccess, error: sileoError } }))

import InboundDelegationCard from './InboundDelegationCard'
import type { InboundDelegation } from '../api/types'

const delegation: InboundDelegation = {
  message_id: 'msg-1',
  from_employee_id: 'ana@empresa.com',
  body: 'Revisa el contrato adjunto y responde al cliente.',
  issued_at: '2026-07-01T10:00:00Z',
  created_at: '2026-07-01T10:00:00Z',
}

function clickButton(container: HTMLElement, label: string) {
  const button = Array.from(container.querySelectorAll('button'))
    .find((b) => b.textContent === label)
  if (!button) throw new Error(`button "${label}" not found`)
  act(() => {
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
}

describe('InboundDelegationCard', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    resolveInboundDelegation.mockReset()
    sileoSuccess.mockReset()
    sileoError.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  it('renders the cross-human framing with the colleague name and the ask', () => {
    act(() => {
      root.render(
        React.createElement(InboundDelegationCard, { delegation, onResolved: vi.fn() }),
      )
    })

    expect(container.textContent).toContain('ana@empresa.com')
    expect(container.textContent).toContain(delegation.body)
  })

  it('approves: calls resolveInboundDelegation(approve) and onResolved', async () => {
    resolveInboundDelegation.mockResolvedValue({ ok: true, task_id: 'task-1' })
    const onResolved = vi.fn()

    act(() => {
      root.render(
        React.createElement(InboundDelegationCard, { delegation, onResolved }),
      )
    })

    clickButton(container, 'Aprobar')
    await act(async () => { await Promise.resolve() })

    expect(resolveInboundDelegation).toHaveBeenCalledWith('msg-1', 'approve')
    expect(onResolved).toHaveBeenCalledTimes(1)
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
  })

  it('rejects: calls resolveInboundDelegation(reject) and onResolved', async () => {
    resolveInboundDelegation.mockResolvedValue({ ok: true })
    const onResolved = vi.fn()

    act(() => {
      root.render(
        React.createElement(InboundDelegationCard, { delegation, onResolved }),
      )
    })

    clickButton(container, 'Rechazar')
    await act(async () => { await Promise.resolve() })

    expect(resolveInboundDelegation).toHaveBeenCalledWith('msg-1', 'reject')
    expect(onResolved).toHaveBeenCalledTimes(1)
  })

  it('shows an inline error and does NOT call onResolved when the API call fails', async () => {
    resolveInboundDelegation.mockRejectedValue(new Error('network down'))
    const onResolved = vi.fn()

    act(() => {
      root.render(
        React.createElement(InboundDelegationCard, { delegation, onResolved }),
      )
    })

    clickButton(container, 'Aprobar')
    await act(async () => { await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    expect(onResolved).not.toHaveBeenCalled()
    expect(sileoError).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('No se pudo aprobar la delegación')
  })
})
