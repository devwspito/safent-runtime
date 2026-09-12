import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as the other tests in this project (no
// @testing-library). useAdsAvailability is mocked per-test so every FR-003
// state renders deterministically — no timers/network involved here (that
// is useAdsAvailability's own test file's job).

const { useAdsAvailability } = vi.hoisted(() => ({
  useAdsAvailability: vi.fn(),
}))

vi.mock('../hooks/useAdsAvailability', () => ({ useAdsAvailability }))

import AdsView from './AdsView'
import type { AdsAvailability } from '../hooks/useAdsAvailability'
import { adsPolicyFixture } from '../api/managedAds.fixtures'

function noop() { /* refresh stub */ }

function setAvailability(status: AdsAvailability['status'], reason: AdsAvailability['reason'] = null) {
  useAdsAvailability.mockReturnValue({ status, reason, refresh: noop })
}

describe('AdsView', () => {
  it('managed mode mounts assignments, never the local iframe', () => {
    useAdsAvailability.mockReturnValue({ status: 'managed', reason: null, policy: adsPolicyFixture, refresh: noop })
    render()
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toContain('Administrado por Enterprise')
    expect(container.querySelector('select')?.value).toBe('')
  })
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    useAdsAvailability.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  function render() {
    act(() => {
      root.render(React.createElement(MemoryRouter, null, React.createElement(AdsView)))
    })
  }

  it('shows a loading state before the first availability response — never blank', () => {
    setAvailability('loading')
    render()

    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toContain('Comprobando el servicio de anuncios')
  })

  it('renders the same-origin iframe (src="/ads/", no query string / secret) when ready', () => {
    setAvailability('ready')
    render()

    const iframe = container.querySelector('iframe')
    expect(iframe).not.toBeNull()
    expect(iframe?.getAttribute('src')).toBe('/ads/')
  })

  it.each([
    ['not_installed', 'El servicio de anuncios no está instalado', 'Ir a Herramientas'],
    ['unreachable', 'No se pudo conectar con el servicio de anuncios', 'Reintentar'],
    ['unauthorized', 'El servicio de anuncios necesita configuración', 'Ir a Herramientas'],
  ] as const)(
    'shows the honest blocked state (never a generic error) for %s',
    (reason, expectedTitle, expectedAction) => {
      setAvailability('unavailable', reason)
      render()

      expect(container.querySelector('iframe')).toBeNull()
      expect(container.textContent).toContain(expectedTitle)
      const button = Array.from(container.querySelectorAll('button'))
        .find((b) => b.textContent?.includes(expectedAction))
      expect(button).not.toBeUndefined()
    },
  )

  it('renders the iframe (not blocked) when unavailable + no_accounts — Ads stays usable', () => {
    setAvailability('unavailable', 'no_accounts')
    render()

    expect(container.querySelector('iframe')).not.toBeNull()
    expect(container.textContent).toContain('Conecta tus cuentas de Google o Meta')
  })

  it('the "unreachable" retry button calls availability.refresh(), not a page reload', () => {
    const refresh = vi.fn()
    useAdsAvailability.mockReturnValue({ status: 'unavailable', reason: 'unreachable', refresh })
    render()

    const button = Array.from(container.querySelectorAll('button'))
      .find((b) => b.textContent?.includes('Reintentar'))!
    act(() => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('shows document loading and reloads only on explicit request, without putting credentials in the URL', () => {
    setAvailability('ready')
    render()
    const first = container.querySelector('iframe')!
    expect(container.textContent).toContain('Abriendo el panel')
    act(() => first.dispatchEvent(new Event('load')))
    expect(container.textContent).not.toContain('Abriendo el panel')
    const reload = [...container.querySelectorAll('button')].find(button => button.textContent?.includes('Recargar panel'))!
    act(() => reload.click())
    expect(container.querySelector('iframe')).not.toBe(first)
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe('/ads/')
    expect(container.textContent).toContain('Abriendo el panel')
  })

  it('removes the panel on loss of authorization and does not restore its previous document state', () => {
    setAvailability('ready')
    render()
    act(() => container.querySelector('iframe')!.dispatchEvent(new Event('load')))
    setAvailability('unavailable', 'unauthorized')
    render()
    expect(container.querySelector('iframe')).toBeNull()
    setAvailability('ready')
    render()
    expect(container.textContent).toContain('Abriendo el panel')
  })
})
