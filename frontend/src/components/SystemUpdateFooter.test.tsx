import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as the rest of this project — no @testing-library.
// api/client is mocked so every SC-006 branch (available/not available/
// updating/failed) renders deterministically without network or timers.

const {
  getSystemUpdate, requestSystemUninstall, postInstallRequest, getInstallRequests,
} = vi.hoisted(() => ({
  getSystemUpdate: vi.fn(),
  requestSystemUninstall: vi.fn(),
  postInstallRequest: vi.fn(),
  getInstallRequests: vi.fn(),
}))

vi.mock('../api/client', () => ({
  getSystemUpdate, requestSystemUninstall, postInstallRequest, getInstallRequests,
}))

import { SystemUpdateFooter } from './SystemUpdateFooter'

function status(overrides: Partial<Awaited<ReturnType<typeof getSystemUpdate>>> = {}) {
  return {
    current_version: '0.8.0',
    latest_version: null,
    update_available: false,
    updating: false,
    ...overrides,
  }
}

describe('SystemUpdateFooter', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
    getSystemUpdate.mockReset().mockResolvedValue(status())
    requestSystemUninstall.mockReset().mockResolvedValue({ ok: true })
    postInstallRequest.mockReset()
    getInstallRequests.mockReset().mockResolvedValue({ requests: [] })
    delete (window as unknown as Record<string, unknown>).__safentUpdate
    delete (window as unknown as Record<string, unknown>).__safentLatestVersion
    delete (window as unknown as Record<string, unknown>).__safentNativeUpdater
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
    vi.unstubAllGlobals()
  })

  async function render() {
    await act(async () => {
      root.render(React.createElement(SystemUpdateFooter))
      await Promise.resolve()
      await Promise.resolve()
    })
  }

  it('shows an explicit check before the first status response arrives', async () => {
    getSystemUpdate.mockReturnValue(new Promise(() => { /* never resolves */ }))
    await render()
    expect(container.textContent).toContain('Comprobando estado')
  })

  it('SC-006: shows the version quietly with NO Actualizar button when nothing is newer', async () => {
    await render()

    expect(container.textContent).toContain('Versión 0.8.0')
    const buttons = Array.from(container.querySelectorAll('button'))
    expect(buttons.some(b => b.textContent?.includes('Actualizar'))).toBe(false)
    // Desinstalar is unrelated to update availability — always present.
    expect(buttons.some(b => b.getAttribute('aria-label') === 'Desinstalar')).toBe(true)
  })

  it('ignores the unsigned legacy VERSION string', async () => {
    (window as unknown as Record<string, unknown>).__safentLatestVersion = '99.0.0'
    await render()
    expect(container.textContent).not.toContain('99.0.0')
    expect(container.textContent).not.toContain('Actualizar')
  })

  it('shows native updater unavailability even while the daemon status is unknown', async () => {
    getSystemUpdate.mockReturnValue(new Promise(() => {}))
    ;(window as unknown as Record<string, unknown>).__safentNativeUpdater = {
      status: 'unavailable', reason: 'integration_missing', app_version: '0.9.0',
    }
    await render()
    expect(container.textContent).toContain('App nativa 0.9.0')
    expect(container.textContent).toContain('no está disponible en esta compilación')
    expect(container.querySelector('button')).toBeNull()
  })

  it('does not hide an independently available engine update because the native updater is unavailable', async () => {
    getSystemUpdate.mockResolvedValue(status({ update_available: true, latest_version: '0.8.1' }))
    ;(window as unknown as Record<string, unknown>).__safentNativeUpdater = {
      status: 'unavailable', reason: 'integration_missing', app_version: '0.9.0',
    }
    await render()
    expect(container.textContent).toContain('App nativa 0.9.0')
    expect(container.textContent).toContain('Versión 0.8.0')
    expect(container.textContent).toContain('v0.8.1')
    expect(container.textContent).toContain('Actualizar')
  })

  it('SC-006: shows Actualizar when the daemon confirms update_available', async () => {
    getSystemUpdate.mockResolvedValue(status({ update_available: true, latest_version: '0.9.0' }))
    await render()

    const buttons = Array.from(container.querySelectorAll('button'))
    expect(buttons.some(b => b.textContent?.includes('Actualizar'))).toBe(true)
    expect(container.textContent).toContain('v0.9.0')
  })

  it('shows Actualizar when the host shell confirms via window.__safentUpdate, even if the daemon check was blocked', async () => {
    (window as unknown as Record<string, unknown>).__safentUpdate = {
      available: true,
      current: { app: '0.8.0', engine: '0.8.0', companion: null },
      to: { app: '0.9.0', engine: '0.9.0', companion: null },
      checked_at: new Date().toISOString(),
    }
    await render()

    const buttons = Array.from(container.querySelectorAll('button'))
    expect(buttons.some(b => b.textContent?.includes('Actualizar'))).toBe(true)
    expect(container.textContent).toContain('v0.9.0')
  })

  it('one click posts the update_system install-request and shows the relaunch notice while installing', async () => {
    getSystemUpdate.mockResolvedValue(status({ update_available: true, latest_version: '0.9.0' }))
    const pendingRequest = {
      verb: 'update_system' as const,
      state: 'pending' as const,
      expires_at: new Date().toISOString(),
    }
    postInstallRequest.mockResolvedValue({ accepted: true, request: pendingRequest })
    await render()
    // Once the request is live, a real backend's GET reflects it too — the
    // effect re-polls immediately on the pending→updating transition.
    getInstallRequests.mockResolvedValue({ requests: [pendingRequest] })

    const updateBtn = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Actualizar'))!
    await act(async () => {
      updateBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    // ConfirmDialog portals to document.body, outside `container` — query the
    // alertdialog directly for its own confirm button.
    const dialog = document.querySelector('[role="alertdialog"]')!
    const confirmBtn = Array.from(dialog.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Actualizar'))!
    await act(async () => {
      confirmBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      for (let i = 0; i < 8; i++) await Promise.resolve()
    })

    expect(postInstallRequest).toHaveBeenCalledWith('update_system')
    expect(container.textContent).toContain('Se cerrará y volverá a abrirse')
  })

  it('a failed update shows safe failure copy and a reviewed retry, never a stuck spinner', async () => {
    getSystemUpdate.mockResolvedValue(status())
    getInstallRequests.mockResolvedValue({
      requests: [{
        verb: 'update_system',
        state: 'failed',
        expires_at: new Date().toISOString(),
        last_failure: { code: 'pull_interrupted', label: 'Se cortó la descarga', retryable: true },
      }],
    })
    await render()

    expect(container.textContent).toContain('No se pudo completar la actualización')
    const retryBtn = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Reintentar'))
    expect(retryBtn).not.toBeUndefined()
    await act(async () => { retryBtn!.click() })
    expect(document.querySelector('[role=alertdialog]')).not.toBeNull()
    expect(postInstallRequest).not.toHaveBeenCalled()
  })

  it('exposes a recoverable failed check without pretending no update exists', async () => {
    getSystemUpdate.mockRejectedValueOnce(new Error('private network error'))
    await render()
    expect(container.textContent).toContain('No se pudo comprobar el estado')
    expect(container.textContent).not.toContain('private network')
    await act(async () => { container.querySelector<HTMLButtonElement>('button')!.click() })
    expect(getSystemUpdate).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain('Versión 0.8.0')
    expect(container.textContent).not.toContain('No se pudo comprobar')
  })

  it('never repeats a POST while its acknowledgement is pending, including a second click on confirm', async () => {
    getSystemUpdate.mockResolvedValue(status({ update_available: true, latest_version: '0.9.0' }))
    let finish!: (value: unknown) => void
    postInstallRequest.mockReturnValueOnce(new Promise(yes => { finish = yes }))
    await render()
    const update = container.querySelector<HTMLButtonElement>('button')!
    await act(async () => { update.click(); update.click() })
    const confirm = document.querySelector<HTMLButtonElement>('.confirm-card__actions button:last-child')!
    await act(async () => { confirm.click(); confirm.click() })
    await act(async () => { update.click() })
    expect(postInstallRequest).toHaveBeenCalledExactlyOnceWith('update_system')
    expect(container.textContent).toContain('Solicitud pendiente')
    const pending = { verb: 'update_system', state: 'pending', expires_at: 'qa' }
    getInstallRequests.mockResolvedValue({ requests: [pending] })
    await act(async () => { finish({ accepted: true, request: pending }) })
    expect(container.textContent).toContain('Se cerrará')
  })

  it('does not turn an unconfirmed update response into success or replay it automatically', async () => {
    getSystemUpdate.mockResolvedValue(status({ update_available: true }))
    postInstallRequest.mockResolvedValue({ accepted: true })
    await render()
    await act(async () => { container.querySelector<HTMLButtonElement>('button')!.click() })
    await act(async () => { document.querySelector<HTMLButtonElement>('.confirm-card__actions button:last-child')!.click() })
    expect(container.textContent).toContain('No se pudo confirmar la solicitud')
    expect(container.textContent).toContain('Comprobar estado')
    expect(postInstallRequest).toHaveBeenCalledOnce()
  })

  it('does not offer retry when the host declares the failure non-retryable', async () => {
    getInstallRequests.mockResolvedValue({ requests: [{ verb: 'update_system', state: 'failed', expires_at: 'qa', last_failure: { retryable: false } }] })
    await render()
    expect(container.textContent).not.toContain('Reintentar')
  })

  it('requires a new review when the checked version changes while confirmation is open', async () => {
    vi.useFakeTimers()
    try {
      getSystemUpdate.mockResolvedValue(status({ update_available: true, latest_version: '0.9.0' }))
      await render()
      await act(async () => { container.querySelector<HTMLButtonElement>('button')!.click() })
      getSystemUpdate.mockResolvedValue(status({ update_available: true, latest_version: '0.10.0' }))
      await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60_000) })
      await act(async () => { document.querySelector<HTMLButtonElement>('.confirm-card__actions button:last-child')!.click() })
      expect(postInstallRequest).not.toHaveBeenCalled()
      expect(container.textContent).toContain('El estado cambió durante la revisión')
    } finally { vi.useRealTimers() }
  })

  it('preserves the last known active request when its status check fails', async () => {
    vi.useFakeTimers()
    try {
      getInstallRequests.mockResolvedValue({ requests: [{ verb: 'update_system', state: 'claimed', expires_at: 'qa' }] })
      await render()
      await act(async () => { await Promise.resolve() })
      getInstallRequests.mockRejectedValueOnce(new Error('offline'))
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
      expect(getInstallRequests).toHaveBeenCalledTimes(3)
      expect(container.textContent).toContain('No se pudo comprobar el estado')
      expect(container.querySelector('button[aria-label="Actualizar"]')?.getAttribute('aria-disabled')).toBe('true')
      expect(postInstallRequest).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })
})
