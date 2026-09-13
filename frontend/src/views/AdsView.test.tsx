import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as the other tests in this project (no
// @testing-library). useAdsAvailability is mocked per-test so every FR-003
// state renders deterministically — no timers/network involved here (that
// is useAdsAvailability's own test file's job).

const { useAdsAvailability, useFeatures } = vi.hoisted(() => ({
  useAdsAvailability: vi.fn(),
  useFeatures: vi.fn(),
}))

vi.mock('../hooks/useAdsAvailability', () => ({ useAdsAvailability }))
vi.mock('../hooks/useFeatures', () => ({ useFeatures }))

import AdsView from './AdsView'
import type { AdsAvailability } from '../hooks/useAdsAvailability'
import { adsPolicyFixture } from '../api/managedAds.fixtures'
import { I18nProvider } from '../lib/i18n'

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
    expect(container.textContent).not.toContain('Configurar conexiones')
    expect(useFeatures).not.toHaveBeenCalled()
    expect(container.querySelector('select')?.value).toBe('')
  })
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
    useAdsAvailability.mockReset()
    useFeatures.mockReset().mockReturnValue({ edition: 'community', isLoading: false, allowed: (view: string) => view === 'integraciones' })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
    vi.unstubAllGlobals()
    localStorage.clear()
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
    expect(container.textContent).not.toContain('Configurar conexiones')
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
      expect(container.textContent).not.toContain('Configurar conexiones')
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

  it.each(['ready', 'no_accounts'])('offers the central connection setup from the Community host with %s', state => {
    setAvailability(state === 'ready' ? 'ready' : 'unavailable', state === 'ready' ? null : 'no_accounts')
    render()
    const link = container.querySelector('a[href="/capacidades?tab=integraciones"]')!
    expect(link.textContent).toBe('Configurar conexiones')
    expect(link.getAttribute('target')).toBeNull()
    expect(link.closest('iframe')).toBeNull()
    expect(container.textContent).toContain('Las conexiones de Anuncios se preparan en Integraciones.')
    expect(container.textContent).not.toMatch(/ya autorizad|conexiones activas|App Secret/)
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe('/ads/')
  })

  it.each([
    { edition: 'community', isLoading: true, allowed: () => true },
    { edition: 'community', isLoading: false, allowed: () => false },
    { edition: 'associate', isLoading: false, allowed: () => true },
  ])('hides the setup shortcut when Community permission is absent or unresolved (case %#)', features => {
    useFeatures.mockReturnValue(features)
    setAvailability('ready')
    render()
    expect(container.querySelector('iframe')).not.toBeNull()
    expect(container.querySelector('a[href="/capacidades?tab=integraciones"]')).toBeNull()
    expect(container.textContent).not.toContain('Las conexiones de Anuncios se preparan')
  })

  it('removes the setup shortcut when the Integrations grant is withdrawn', () => {
    setAvailability('ready')
    render()
    expect(container.querySelector('a[href="/capacidades?tab=integraciones"]')).not.toBeNull()
    useFeatures.mockReturnValue({ edition: 'community', isLoading: false, allowed: () => false })
    render()
    expect(container.querySelector('a[href="/capacidades?tab=integraciones"]')).toBeNull()
  })

  it('navigates the host router to the confirmed Integrations tab without changing iframe security', () => {
    function Destination() {
      const location = useLocation()
      return <p>{location.pathname}{location.search}</p>
    }
    setAvailability('ready')
    act(() => {
      root.render(<MemoryRouter initialEntries={['/anuncios']}><Routes>
        <Route path="/anuncios" element={<AdsView />} />
        <Route path="/capacidades" element={<Destination />} />
      </Routes></MemoryRouter>)
    })
    const link = container.querySelector<HTMLAnchorElement>('a[href="/capacidades?tab=integraciones"]')!
    expect(link).not.toBeNull()
    act(() => link.click())
    expect(container.textContent).toBe('/capacidades?tab=integraciones')
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('shows the central setup guidance in English', () => {
    setAvailability('ready')
    localStorage.setItem('safent_ui_locale', 'en')
    act(() => { root.render(<I18nProvider><MemoryRouter><AdsView /></MemoryRouter></I18nProvider>) })
    expect(container.textContent).toContain('Ads connections are set up in Integrations.')
    expect(container.querySelector('a[href="/capacidades?tab=integraciones"]')?.textContent).toBe('Configure connections')
  })
})
