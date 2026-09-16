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
import { I18nProvider } from '../lib/i18n'

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
    localStorage.clear()
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
    localStorage.clear()
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

  it('checks on demand before offering Update, then asks native to recheck and confirm without arguments', async () => {
    const invoke = vi.fn().mockResolvedValueOnce({ status: 'available', app_version: '0.9.19', version: '0.9.20' }).mockResolvedValue(undefined)
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status:'available', reason:'app_only', app_version:'0.9.19' })
    getSystemUpdate.mockResolvedValue(status({ update_available:true, latest_version:'99.0.0' }))
    await render()
    expect(container.textContent).toContain('App nativa 0.9.19')
    const button = container.querySelector('button')!
    expect(button.textContent).toBe('Buscar actualizaciones')
    expect(invoke).not.toHaveBeenCalled()
    expect(getSystemUpdate).not.toHaveBeenCalled()
    expect(getInstallRequests).not.toHaveBeenCalled()
    await act(async () => { button.click() })
    expect(invoke).toHaveBeenCalledExactlyOnceWith('get_native_update_status')
    expect(button.textContent).toBe('Actualizar')
    expect(container.querySelector('[role=status]')?.textContent).toBe('Versión 0.9.20 disponible')
    expect(container.textContent).not.toContain('Solicitud enviada')
    expect(postInstallRequest).not.toHaveBeenCalled()
    expect(container.textContent).not.toContain('99.0.0')
    await act(async () => { button.click() })
    expect(invoke.mock.calls).toEqual([['get_native_update_status'], ['show_native_updater']])
    expect(container.textContent).toContain('Solicitud enviada al actualizador')
    expect(container.textContent).toContain('la instalación requiere tu confirmación')
    expect(button.textContent).toBe('Buscar actualizaciones')
    expect(container.textContent).not.toContain('0.9.20 disponible')
    expect(postInstallRequest).not.toHaveBeenCalled()
    expect(document.querySelector('[role=alertdialog]')).toBeNull()
  })

  it('does not poll or automatically invoke the native updater', async () => {
    vi.useFakeTimers()
    try {
      const invoke = vi.fn().mockResolvedValue(undefined)
      vi.stubGlobal('__TAURI__', { core: { invoke } })
      vi.stubGlobal('__safentNativeUpdater', { status:'available', reason:'app_only', app_version:'0.9.19' })
      await render()
      await act(async () => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')) })
      await act(async () => { await vi.advanceTimersByTimeAsync(60 * 60_000) })
      expect(invoke).not.toHaveBeenCalled()
      expect(getSystemUpdate).not.toHaveBeenCalled()
      expect(getInstallRequests).not.toHaveBeenCalled()
      expect(postInstallRequest).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('singleflights the native check and safely offers retry after IPC failure', async () => {
    let reject!:(error:Error) => void
    const invoke = vi.fn().mockReturnValueOnce(new Promise((_, fail) => { reject = fail })).mockResolvedValue({ status: 'up_to_date', app_version: '0.9.19', version: null })
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status:'available', reason:'app_only', app_version:'0.9.19' })
    await render()
    const button = container.querySelector('button')!
    await act(async () => { button.click(); button.click() })
    expect(invoke).toHaveBeenCalledTimes(1)
    expect(button.disabled).toBe(true)
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(button.textContent).toBe('Buscando…')
    await act(async () => { reject(new Error('private IPC details')) })
    expect(container.textContent).toContain('No se pudieron buscar actualizaciones')
    expect(container.textContent).not.toContain('private IPC details')
    expect(button.disabled).toBe(false)
    await act(async () => { button.click() })
    expect(invoke).toHaveBeenCalledTimes(2)
    expect(invoke.mock.calls).toEqual([['get_native_update_status'], ['get_native_update_status']])
    expect(button.textContent).toBe('Buscar actualizaciones')
    expect(container.querySelector('[role=status]')?.textContent).toBe('Safent está actualizado')
    expect(container.querySelector('[role=alert]')).toBeNull()
    expect(postInstallRequest).not.toHaveBeenCalled()
  })

  it('does not fall back to the engine updater when the native bridge is missing', async () => {
    vi.stubGlobal('__TAURI__', undefined)
    vi.stubGlobal('__safentNativeUpdater', { status:'available', reason:'app_only', app_version:'0.9.19' })
    await render()
    await act(async () => { container.querySelector('button')!.click() })
    expect(container.textContent).toContain('No se pudieron buscar actualizaciones')
    expect(postInstallRequest).not.toHaveBeenCalled()
    expect(getSystemUpdate).not.toHaveBeenCalled()
  })

  it('distinguishes native updater launch failure from search failure and singleflights the launch', async () => {
    let reject!: (error: Error) => void
    const invoke = vi.fn()
      .mockResolvedValueOnce({ status: 'available', app_version: '0.9.19', version: '0.9.20' })
      .mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
      .mockResolvedValue(null)
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.19' })
    await render()
    const button = container.querySelector('button')!
    await act(async () => { button.click() })
    await act(async () => { button.click(); button.click() })
    expect(button.disabled).toBe(true)
    expect(button.textContent).toBe('Abriendo actualizador…')
    expect(invoke.mock.calls).toEqual([['get_native_update_status'], ['show_native_updater']])
    await act(async () => { reject(new Error('private native details')) })
    expect(container.querySelector('[role=alert]')?.textContent).toContain('No se pudo abrir el actualizador')
    expect(container.textContent).not.toContain('private native details')
    expect(container.textContent).not.toContain('Solicitud enviada')
    expect(button.disabled).toBe(false)
    expect(button.textContent).toBe('Actualizar')
    await act(async () => { button.click() })
    expect(invoke.mock.calls).toEqual([['get_native_update_status'], ['show_native_updater'], ['show_native_updater']])
    expect(container.querySelector('[role=alert]')).toBeNull()
    expect(container.textContent).toContain('Solicitud enviada al actualizador')
    expect(postInstallRequest).not.toHaveBeenCalled()
    expect(getSystemUpdate).not.toHaveBeenCalled()
  })

  it('keeps Check available after up-to-date and requires another deliberate check to discover a version', async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce({ status: 'up_to_date', app_version: '0.9.19', version: null })
      .mockResolvedValueOnce({ status: 'available', app_version: '0.9.19', version: '0.9.21' })
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.19' })
    await render()
    const button = container.querySelector('button')!
    await act(async () => { button.click() })
    expect(container.textContent).toContain('Safent está actualizado')
    expect(button.textContent).toBe('Buscar actualizaciones')
    await act(async () => { button.click() })
    expect(container.textContent).not.toContain('Safent está actualizado')
    expect(container.textContent).toContain('Versión 0.9.21 disponible')
    expect(button.textContent).toBe('Actualizar')
    expect(invoke.mock.calls).toEqual([['get_native_update_status'], ['get_native_update_status']])
  })

  it.each([
    null, [], {}, { status: 'unknown', app_version: '0.9.19', version: null },
    { status: 'up_to_date', app_version: '0.9.19' },
    { status: 'up_to_date', app_version: '0.9.19', version: '0.9.20' },
    { status: 'available', app_version: '0.9.18', version: '0.9.20' },
    { status: 'available', version: '0.9.20' },
    { status: 'available', app_version: '0.9.19', version: null },
    { status: 'available', app_version: '0.9.19', version: '0.9.20', check_id: 'not-an-install-permission' },
    { status: 'available', app_version: '0.9.19', version: '0.9.20', artifacts: ['private-artifact'] },
  ])('fails closed on an unknown or malformed native status (case %#)', async response => {
    const invoke = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.19' })
    await render()
    await act(async () => { container.querySelector('button')!.click() })
    expect(container.querySelector('[role=alert]')?.textContent).toContain('No se pudieron buscar actualizaciones')
    expect(container.querySelector('button')!.textContent).toBe('Buscar actualizaciones')
    expect(container.querySelector('button')!.disabled).toBe(false)
    expect(container.textContent).not.toMatch(/Safent está actualizado|Versión .* disponible|private-artifact|not-an-install-permission/)
    expect(invoke).toHaveBeenCalledExactlyOnceWith('get_native_update_status')
    expect(getSystemUpdate).not.toHaveBeenCalled()
    expect(postInstallRequest).not.toHaveBeenCalled()
  })

  it.each(['', 'v0.9.20', '0.9', '0.9.20<script>', '00.9.20', '0.9.20-01', '0.9.20_bad', '0.9.20+abc..def', `0.9.20+${'a'.repeat(60)}`, '0.9.18', '0.9.19', '0.9.19+build', '0.9.19-rc.1'])(
    'does not offer an invalid, old or equal native version: %s', async version => {
      const invoke = vi.fn().mockResolvedValue({ status: 'available', app_version: '0.9.19', version })
      vi.stubGlobal('__TAURI__', { core: { invoke } })
      vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.19' })
      await render()
      await act(async () => { container.querySelector('button')!.click() })
      expect(container.querySelector('[role=alert]')).not.toBeNull()
      expect(container.querySelector('button')!.textContent).toBe('Buscar actualizaciones')
      expect(postInstallRequest).not.toHaveBeenCalled()
    },
  )

  it.each([
    ['0.9.19', '0.9.20'], ['0.9.19', '0.10.0'], ['0.9.19', '1.0.0+build'],
    ['0.9.20-rc.1', '0.9.20'], ['0.9.20-rc.9', '0.9.20-rc.10'],
    ['0.9.20-alpha', '0.9.20-beta'], ['0.9.20-1', '0.9.20-alpha'], ['0.9.20-rc', '0.9.20-rc.1'],
  ])('offers a confirmed newer native semver from %s to %s', async (appVersion, version) => {
    const invoke = vi.fn().mockResolvedValue({ status: 'available', app_version: appVersion, version })
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: appVersion })
    await render()
    await act(async () => { container.querySelector('button')!.click() })
    expect(container.querySelector('button')!.textContent).toBe('Actualizar')
    expect(container.querySelector('[role=alert]')).toBeNull()
    expect(container.querySelector('[role=status]')?.textContent).toBe(`Versión ${version} disponible`)
  })

  it('ignores a late status response after the native app context changes', async () => {
    let finish!: (response: unknown) => void
    const invoke = vi.fn().mockReturnValue(new Promise(resolve => { finish = resolve }))
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.19' })
    await render()
    await act(async () => { container.querySelector('button')!.click() })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.20' })
    await render()
    await act(async () => { finish({ status: 'available', app_version: '0.9.19', version: '0.9.21' }) })
    expect(container.textContent).toContain('App nativa 0.9.20')
    expect(container.textContent).not.toContain('0.9.21')
    expect(container.querySelector('button')!.textContent).toBe('Buscar actualizaciones')
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it('renders the native check outcome in English', async () => {
    localStorage.setItem('safent_ui_locale', 'en')
    const invoke = vi.fn().mockResolvedValue({ status: 'up_to_date', app_version: '0.9.19', version: null })
    vi.stubGlobal('__TAURI__', { core: { invoke } })
    vi.stubGlobal('__safentNativeUpdater', { status: 'available', reason: 'app_only', app_version: '0.9.19' })
    await act(async () => { root.render(<I18nProvider><SystemUpdateFooter /></I18nProvider>) })
    expect(container.querySelector('button')!.textContent).toBe('Check for updates')
    await act(async () => { container.querySelector('button')!.click() })
    expect(container.querySelector('[role=status]')?.textContent).toBe('Safent is up to date')
  })

  it('shows an unsigned native build honestly without offering independent engine mutation', async () => {
    vi.stubGlobal('__safentNativeUpdater', { status:'unavailable', reason:'signing_configuration_missing', app_version:'0.9.19' })
    await render()
    expect(container.textContent).toContain('App nativa 0.9.19')
    expect(container.textContent).toContain('no está disponible en esta compilación')
    expect(container.querySelector('button')).toBeNull()
    expect(getSystemUpdate).not.toHaveBeenCalled()
    expect(postInstallRequest).not.toHaveBeenCalled()
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
