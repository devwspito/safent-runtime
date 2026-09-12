import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as the rest of this project — no @testing-library.

const { postInstallRequest, getInstallRequests } = vi.hoisted(() => ({
  postInstallRequest: vi.fn(),
  getInstallRequests: vi.fn(),
}))

vi.mock('../api/client', () => ({ postInstallRequest, getInstallRequests }))

import { CompanionInstallAction } from './CompanionInstallAction'
import type { AdsAvailability } from '../hooks/useAdsAvailability'

function availability(
  status: AdsAvailability['status'],
  reason: AdsAvailability['reason'] = null,
): AdsAvailability {
  return { status, reason, refresh: vi.fn() }
}

describe('CompanionInstallAction', () => {
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

  async function render(av: AdsAvailability, compact = false) {
    await act(async () => {
      root.render(React.createElement(CompanionInstallAction, { availability: av, compact }))
      await Promise.resolve()
    })
  }

  it('retries a failed status read without submitting install or repair from unknown state', async () => {
    getInstallRequests.mockRejectedValueOnce(new Error('private failure'))
    await render(availability('unavailable', 'not_installed'))
    expect(container.textContent).toContain('No se pudo comprobar la instalación')
    expect(container.textContent).not.toContain('private failure')
    await act(async () => { container.querySelector<HTMLButtonElement>('button')!.click() })
    expect(getInstallRequests).toHaveBeenCalledTimes(2)
    expect(postInstallRequest).not.toHaveBeenCalled()
    expect(container.textContent).toContain('Instalar')
  })

  it.each([
    ['ready', null] as const,
    ['loading', null] as const,
    ['unavailable', 'unauthorized'] as const,
    ['unavailable', 'no_accounts'] as const,
  ])('renders NOTHING for %s/%s — never fakes an action it cannot take', async (status, reason) => {
    await render(availability(status, reason))
    expect(container.innerHTML).toBe('')
  })

  it('shows "Instalar" for not_installed and posts install_companion when clicked', async () => {
    postInstallRequest.mockResolvedValue({
      accepted: true,
      request: { verb: 'install_companion', state: 'pending', expires_at: 't' },
    })
    await render(availability('unavailable', 'not_installed'))

    const button = container.querySelector('button')!
    expect(button.textContent).toBe('Instalar')
    await act(async () => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(postInstallRequest).toHaveBeenCalledWith('install_companion', { slug: 'safent-ads' })
  })

  it('shows "Reparar" for unreachable (present but down) and posts repair_companion', async () => {
    postInstallRequest.mockResolvedValue({
      accepted: true,
      request: { verb: 'repair_companion', state: 'pending', expires_at: 't' },
    })
    await render(availability('unavailable', 'unreachable'))

    const button = container.querySelector('button')!
    expect(button.textContent).toBe('Reparar')
    await act(async () => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(postInstallRequest).toHaveBeenCalledWith('repair_companion', { slug: 'safent-ads' })
  })

  it('shows honest progress while installing — no button, so a second click is structurally impossible', async () => {
    getInstallRequests.mockResolvedValue({
      requests: [{ verb: 'install_companion', state: 'claimed', expires_at: 't', stage: 'pull_companion' }],
    })
    await render(availability('unavailable', 'not_installed'))

    expect(container.querySelector('button')).toBeNull()
    expect(container.textContent).toContain('Descargando el servicio de anuncios')
  })

  it('a failed install shows the backend-supplied reason and Reintentar — never a fake "conectado"', async () => {
    getInstallRequests.mockResolvedValue({
      requests: [{
        verb: 'install_companion', state: 'failed', expires_at: 't',
        last_failure: { code: 'registry_unreachable', label: 'No se pudo descargar la imagen', retryable: true },
      }],
    })
    await render(availability('unavailable', 'not_installed'))

    expect(container.textContent).toContain('No se pudo descargar la imagen')
    const button = container.querySelector('button')!
    expect(button.textContent).toBe('Reintentar')
  })

  it('compact mode renders only the action, no description paragraph (sidebar real estate)', async () => {
    getInstallRequests.mockResolvedValue({
      requests: [{
        verb: 'install_companion', state: 'failed', expires_at: 't',
        last_failure: { code: 'registry_unreachable', label: 'No se pudo descargar la imagen', retryable: true },
      }],
    })
    await render(availability('unavailable', 'not_installed'), true)

    expect(container.querySelector('p')).toBeNull()
    expect(container.querySelector('button')?.textContent).toBe('Reintentar')
  })
})
