import { JSDOM } from 'jsdom'
import { describe, expect, it, vi } from 'vitest'
import { renderNativeUpdater } from './native-updater.js'

describe('native updater capability', () => {
  const metadata = { status: 'unavailable', reason: 'integration_missing', app_version: '0.9.0' }

  it('shows a build limitation, not latest/up-to-date, without an action or request', () => {
    const element = new JSDOM('<p></p>').window.document.querySelector('p')!
    renderNativeUpdater(element, metadata)
    expect(element.hidden).toBe(false)
    expect(element.textContent).toContain('App nativa 0.9.0')
    expect(element.textContent).toContain('no está disponible en esta compilación')
    expect(element.querySelector('button, a')).toBeNull()
  })

  it.each([undefined, null, {}, { ...metadata, status: 'up_to_date' },
    { ...metadata, reason: 'untrusted' }, { ...metadata, app_version: '<img src=x onerror=alert(1)>' }])(
    'does not invent capability from missing or unsupported metadata %j', value => {
      const element = new JSDOM('<p>Previous value</p>').window.document.querySelector('p')!
      renderNativeUpdater(element, value)
      expect(element.hidden).toBe(true)
      expect(element.textContent).toBe('')
    },
  )
})

describe('app-only native update flow', () => {
  const capability = { status: 'available', reason: 'app_only', app_version: '0.9.1' }
  const available = { status: 'available', app_version: '0.9.1', version: '0.9.2', check_id: 7 }
  const flush = () => new Promise(resolve => setTimeout(resolve, 0))
  const element = () => new JSDOM('<p></p>').window.document.querySelector('p')!

  it('requires check then explicit install; passes only the host check ID', async () => {
    const el = element()
    const invoke = vi.fn().mockResolvedValueOnce(available).mockResolvedValueOnce('cancelled')
    renderNativeUpdater(el, capability, { invoke })
    const button = el.querySelector('button')!
    expect(invoke).not.toHaveBeenCalled()
    button.click()
    button.click()
    expect(invoke).toHaveBeenCalledTimes(1)
    expect(button.disabled).toBe(true)
    await flush()
    expect(el.textContent).toContain('La firma del archivo se verificará')
    expect(button.textContent).toContain('0.9.2')
    button.click()
    await flush()
    expect(invoke).toHaveBeenNthCalledWith(2, 'install_native_update', { checkId: 7 })
    expect(el.textContent).toContain('cancelada')
    expect(button.textContent).toBe('Buscar actualización de la app')
  })

  it.each([null, {}, { ...available, check_id: -1 }, { ...available, app_version: '0.1.0' },
    { ...available, version: '<img src=x onerror=alert(1)>' }, { ...available, status: 'up_to_date' }])(
    'rejects malformed/stale metadata without granting install: %j', async result => {
      const el = element()
      const invoke = vi.fn().mockResolvedValue(result)
      renderNativeUpdater(el, capability, { invoke })
      el.querySelector('button')!.click()
      await flush()
      expect(el.textContent).toContain('No se pudo comprobar')
      expect(el.querySelector('img')).toBeNull()
      expect(el.querySelector('button')!.textContent).not.toContain('Actualizar a')
    },
  )

  it('reports no update only after an actual successful check', async () => {
    const el = element()
    const invoke = vi.fn().mockResolvedValue({ status: 'up_to_date', app_version: '0.9.1', version: null, check_id: null })
    renderNativeUpdater(el, capability, { invoke })
    expect(el.textContent).not.toContain('No hay')
    el.querySelector('button')!.click()
    await flush()
    expect(el.textContent).toContain('No hay una versión más reciente')
  })

  it('discards stale responses after remount and never renders raw host errors', async () => {
    const el = element()
    let resolve!: (value: unknown) => void
    const invoke = vi.fn().mockImplementationOnce(() => new Promise(done => { resolve = done }))
    renderNativeUpdater(el, capability, { invoke })
    el.querySelector('button')!.click()
    renderNativeUpdater(el, capability, { invoke: vi.fn().mockRejectedValue('SECRET https://private') })
    resolve(available)
    await flush()
    expect(el.textContent).not.toContain('0.9.2')
    el.querySelector('button')!.click()
    await flush()
    expect(el.textContent).not.toContain('SECRET')
    expect(el.textContent).toContain('No se pudo comprobar')
  })

  it('consumes failed installation snapshot and never retries automatically', async () => {
    const el = element()
    const invoke = vi.fn().mockResolvedValueOnce(available).mockRejectedValueOnce('update_install_failed')
    renderNativeUpdater(el, capability, { invoke })
    const button = el.querySelector('button')!
    button.click(); await flush()
    button.click(); await flush()
    expect(invoke).toHaveBeenCalledTimes(2)
    expect(button.textContent).toBe('Buscar actualización de la app')
    expect(el.textContent).toContain('Comprueba la versión instalada')
  })
})
