import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as InboundDelegationCard.test.tsx — plain
// react-dom + DOM assertions, no @testing-library. NavLink needs a Router
// context (MemoryRouter); useT() falls back to its default context value
// (locale "es") with no I18nProvider, exactly like the other test in this
// project that calls useT() unwrapped.
//
// not_installed now also mounts CompanionInstallAction (029), which polls
// GET /system/requests via api/client — mocked here so these tests stay
// network-free like the rest of the suite.
const { postInstallRequest, getInstallRequests } = vi.hoisted(() => ({
  postInstallRequest: vi.fn(),
  getInstallRequests: vi.fn(),
}))
vi.mock('../api/client', () => ({ postInstallRequest, getInstallRequests }))

import { AdsNavItem, isAdsBlocked } from './AdsNavItem'
import type { AdsAvailability } from '../hooks/useAdsAvailability'

function noop() { /* AdsAvailability.refresh stub */ }

function availability(
  status: AdsAvailability['status'],
  reason: AdsAvailability['reason'] = null,
): AdsAvailability {
  return { status, reason, refresh: noop }
}

describe('isAdsBlocked', () => {
  it('is false while loading', () => {
    expect(isAdsBlocked(availability('loading'))).toBe(false)
  })

  it('is false when ready', () => {
    expect(isAdsBlocked(availability('ready'))).toBe(false)
  })

  it('is false for "unavailable" + no_accounts (the panel still guides connection)', () => {
    expect(isAdsBlocked(availability('unavailable', 'no_accounts'))).toBe(false)
  })

  it.each(['not_installed', 'unreachable', 'unauthorized'] as const)(
    'is true for "unavailable" + %s',
    (reason) => {
      expect(isAdsBlocked(availability('unavailable', reason))).toBe(true)
    },
  )
})

describe('AdsNavItem', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    postInstallRequest.mockReset()
    getInstallRequests.mockReset().mockResolvedValue({ requests: [] })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  function render(av: AdsAvailability) {
    act(() => {
      root.render(
        React.createElement(
          MemoryRouter,
          null,
          React.createElement('ul', null, React.createElement(AdsNavItem, { availability: av })),
        ),
      )
    })
  }

  it('always renders a clickable/focusable link to /anuncios, in every state', () => {
    for (const av of [
      availability('loading'),
      availability('ready'),
      availability('managed'),
      availability('unavailable', 'not_installed'),
      availability('unavailable', 'unreachable'),
      availability('unavailable', 'unauthorized'),
      availability('unavailable', 'no_accounts'),
    ]) {
      render(av)
      const link = container.querySelector('a[href="/anuncios"]')
      expect(link).not.toBeNull()
      expect(link?.getAttribute('tabindex')).not.toBe('-1')
      expect(container.textContent).toContain('Anuncios')
    }
  })

  it('shows no status dot and no aria-describedby while loading', () => {
    render(availability('loading'))
    const link = container.querySelector('a[href="/anuncios"]')
    expect(link?.getAttribute('aria-describedby')).toBeNull()
    expect(container.querySelector('[data-testid="ads-nav-dot"]')).toBeNull()
  })

  it('shows no status dot when ready', () => {
    render(availability('ready'))
    expect(container.querySelector('[data-testid="ads-nav-dot"]')).toBeNull()
  })

  it('shows no status dot for unavailable + no_accounts (still usable)', () => {
    render(availability('unavailable', 'no_accounts'))
    expect(container.querySelector('[data-testid="ads-nav-dot"]')).toBeNull()
    expect(container.querySelector('a[href="/anuncios"]')?.getAttribute('aria-describedby')).toBeNull()
  })

  it.each([
    ['not_installed', 'El servicio de anuncios no está instalado'],
    ['unreachable', 'No se pudo conectar con el servicio de anuncios'],
    ['unauthorized', 'El servicio de anuncios necesita configuración'],
  ] as const)(
    'shows a status dot + a screen-reader-only reason for unavailable + %s',
    (reason, expectedText) => {
      render(availability('unavailable', reason))

      const link = container.querySelector('a[href="/anuncios"]')
      const dot = container.querySelector('[data-testid="ads-nav-dot"]')
      expect(dot).not.toBeNull()
      expect(dot?.getAttribute('aria-hidden')).toBe('true')

      const describedById = link?.getAttribute('aria-describedby')
      expect(describedById).toBe('ads-nav-status')
      const reasonEl = container.querySelector(`#${describedById}`)
      expect(reasonEl).not.toBeNull()
      expect(reasonEl?.textContent).toBe(expectedText)
      // The dot is decorative only — the reason must be conveyed as real
      // text, never color/shape alone (NFR-004).
      expect(reasonEl?.className).toContain('sr-only')
    },
  )

  it('029 FR-001: not_installed also renders the shared "Instalar" action, alongside the always-present link', async () => {
    postInstallRequest.mockResolvedValue({
      accepted: true,
      request: { verb: 'install_companion', state: 'pending', expires_at: 't' },
    })
    render(availability('unavailable', 'not_installed'))
    await act(async () => { await Promise.resolve() })

    const link = container.querySelector('a[href="/anuncios"]')
    expect(link).not.toBeNull() // the link never disappears, even with the action present
    const installBtn = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent === 'Instalar')
    expect(installBtn).not.toBeUndefined()

    await act(async () => {
      installBtn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    expect(postInstallRequest).toHaveBeenCalledWith('install_companion', { slug: 'safent-ads' })
  })

  it('unreachable/unauthorized do NOT render the install action (only not_installed does)', () => {
    render(availability('unavailable', 'unreachable'))
    expect(Array.from(container.querySelectorAll('button')).some(b => b.textContent === 'Instalar')).toBe(false)
    expect(Array.from(container.querySelectorAll('button')).some(b => b.textContent === 'Reparar')).toBe(false)
  })
})
