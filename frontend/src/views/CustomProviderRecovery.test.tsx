import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ addProvider: vi.fn(), updateProvider: vi.fn(), testProvider: vi.fn(), setActiveProvider: vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('sileo', () => ({ sileo: { success: vi.fn(), error: vi.fn(), warn: vi.fn() } }))
import { CustomProviderCard } from './ProvidersView'

let host: HTMLDivElement, root: Root
const onAdded = vi.fn()
beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  Object.values(api).forEach(f => f.mockReset())
  onAdded.mockReset()
  api.addProvider.mockResolvedValue({ provider_id: 'saved-provider' })
  api.updateProvider.mockResolvedValue({ provider_id: 'saved-provider' })
  api.setActiveProvider.mockResolvedValue({ ok: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  await act(async () => root.render(<CustomProviderCard onAdded={onAdded} onToast={vi.fn()} />))
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
  expect(button).toBeTruthy()
  await act(async () => button.click())
}
async function prepare() {
  await click('Añadir modelo propio')
  host.querySelector<HTMLInputElement>('#pv-c-url')!.value = 'https://model.example.test/v1'
  host.querySelector<HTMLInputElement>('#pv-c-model')!.value = 'example-model'
  host.querySelector<HTMLInputElement>('#pv-c-key')!.value = 'test-secret-not-real'
}
it('retries the saved provider, clears the key and activates only after a successful probe', async () => {
  api.testProvider.mockResolvedValueOnce({ ok: false }).mockResolvedValueOnce({ ok: true })
  await prepare(); await click('Guardar')
  expect(api.addProvider).toHaveBeenCalledTimes(1)
  expect(api.setActiveProvider).not.toHaveBeenCalled()
  expect(onAdded).not.toHaveBeenCalled() // parent reload would destroy retry state
  expect(host.querySelector<HTMLInputElement>('#pv-c-key')!.value).toBe('')
  expect(host.textContent).toContain('Modelo guardado, pero aún no está activo')
  await click('Reintentar conexión')
  expect(api.addProvider).toHaveBeenCalledTimes(1)
  expect(api.updateProvider).toHaveBeenCalledWith('saved-provider', {
    alias: 'example-model', default_model: 'example-model', base_url: 'https://model.example.test/v1',
  })
  expect(api.testProvider).toHaveBeenNthCalledWith(2, 'saved-provider')
  expect(api.setActiveProvider).toHaveBeenCalledExactlyOnceWith('saved-provider')
  expect(onAdded).toHaveBeenCalledTimes(1)
})
it('allows correcting the URL and key without exposing raw upstream errors or creating another row', async () => {
  api.testProvider.mockResolvedValue({ ok: false, code: 'invalid_key', error: 'sensitive-provider-body' })
  await prepare(); await click('Guardar')
  expect(host.textContent).toContain('El servidor rechazó la clave')
  expect(host.textContent).not.toContain('sensitive-provider-body')
  host.querySelector<HTMLInputElement>('#pv-c-url')!.value = 'https://fixed.example.test/v1'
  host.querySelector<HTMLInputElement>('#pv-c-key')!.value = 'replacement-test-secret'
  await click('Reintentar conexión')
  expect(api.addProvider).toHaveBeenCalledTimes(1)
  expect(api.updateProvider).toHaveBeenCalledWith('saved-provider', expect.objectContaining({
    base_url: 'https://fixed.example.test/v1', api_key: 'replacement-test-secret',
  }))
  expect(api.setActiveProvider).not.toHaveBeenCalled()
  expect(host.querySelector<HTMLInputElement>('#pv-c-key')!.value).toBe('')
})
it('does not activate after leaving during the probe', async () => {
  let resolve!: (value: { ok: boolean }) => void
  api.testProvider.mockReturnValue(new Promise(r => { resolve = r }))
  await prepare(); await click('Guardar')
  act(() => root.render(null))
  await act(async () => resolve({ ok: true }))
  expect(api.setActiveProvider).not.toHaveBeenCalled()
})
