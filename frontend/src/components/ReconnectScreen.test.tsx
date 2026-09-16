import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { ReconnectScreen } from './ReconnectScreen'

describe('ReconnectScreen', () => {
  let container: HTMLDivElement
  let root: Root
  let reloadSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    reloadSpy = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { ...window.location, reload: reloadSpy },
      writable: true,
    })
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  it('shows one alert region with a single action — never a stack of errors', () => {
    act(() => {
      root.render(React.createElement(ReconnectScreen, { reason: 'no_token' }))
    })

    const alerts = container.querySelectorAll('[role="alert"]')
    expect(alerts.length).toBe(1)
    expect(container.querySelectorAll('button').length).toBe(1)
    expect(container.textContent).toContain('Reabre Safent')
    expect(document.activeElement).toBe(container.querySelector('h1'))
  })

  it.each([
    ['no_token', 'No se encontró una sesión de Safent en esta ventana.'],
    ['refresh_failed', 'La sesión de Safent ha caducado.'],
  ] as const)('states the honest reason for %s', (reason, expectedText) => {
    act(() => {
      root.render(React.createElement(ReconnectScreen, { reason }))
    })

    expect(container.textContent).toContain(expectedText)
  })

  it('the action reloads the window — a clean re-entry, not a background retry', () => {
    act(() => {
      root.render(React.createElement(ReconnectScreen, { reason: 'no_token' }))
    })

    const button = container.querySelector('button')!
    act(() => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })
})
