import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, getNativeModelCatalog, selectNativeModel } from '../api/client'
import { I18nProvider } from '../lib/i18n'
import type { Provider } from '../api/types'
import NativeModelSelector from './NativeModelSelector'

const access = vi.hoisted(() => ({ allowed: true }))
vi.mock('../hooks/useFeatures', () => ({ useFeatures: () => ({ allowed: () => access.allowed }) }))
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, getNativeModelCatalog: vi.fn(), selectNativeModel: vi.fn() }
})
let host: HTMLDivElement
let root: Root
let provider: Provider
const changed = vi.fn()
const catalog = { provider_id: 'openai-codex' as const, active_model: 'account-astra', models: ['account-astra', 'account-terra'] }
async function render() { await act(async () => { root.render(<I18nProvider><NativeModelSelector provider={provider} onChanged={changed} /></I18nProvider>) }) }
function button(label: string) { return [...host.querySelectorAll('button')].find(node => node.textContent === label)! }
async function click(label: string) { await act(async () => { button(label).click() }) }
async function choose(model: string) {
  await act(async () => { const node = host.querySelector('select')!; node.value = model; node.dispatchEvent(new Event('change', { bubbles: true })) })
}
beforeEach(() => {
  localStorage.clear(); vi.resetAllMocks(); access.allowed = true
  provider = { provider_id: 'openai-codex', default_model: 'account-astra', is_active: true }
  vi.mocked(getNativeModelCatalog).mockResolvedValue(catalog)
  vi.mocked(selectNativeModel).mockResolvedValue({ provider_id: 'openai-codex', active_model: 'account-terra' })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('queries only on request and saves a discovered account model without OAuth or automatic writes', async () => {
  await render(); expect(getNativeModelCatalog).not.toHaveBeenCalled()
  await click('Cambiar modelo')
  expect([...host.querySelectorAll('option')].map(node => node.value)).toEqual(catalog.models)
  expect(document.activeElement).toBe(host.querySelector('select'))
  expect(host.textContent).toContain('No necesitas volver a iniciar sesión')
  expect(selectNativeModel).not.toHaveBeenCalled()
  await choose('account-terra'); expect(selectNativeModel).not.toHaveBeenCalled()
  await click('Usar este modelo')
  expect(selectNativeModel).toHaveBeenCalledExactlyOnceWith({ provider_id: 'openai-codex', active_model: 'account-astra', model: 'account-terra' })
  expect(changed).toHaveBeenCalledOnce()
  expect(host.querySelector('[role=status]')?.textContent).toContain('Tu conexión se conserva')
})

it('does not present an unlisted current or hardcoded model as an available new choice', async () => {
  vi.mocked(getNativeModelCatalog).mockResolvedValue({ ...catalog, models: ['account-terra'] })
  await render(); await click('Cambiar modelo')
  expect(host.querySelector('select')?.value).toBe('')
  expect((button('Usar este modelo') as HTMLButtonElement).disabled).toBe(true)
  expect([...host.querySelectorAll<HTMLOptionElement>('option:not(:disabled)')].map(node => node.value)).toEqual(['account-terra'])
  expect(host.textContent).not.toContain('gpt-6-astra')
})

it.each([401, 403, 409, 503])('shows safe retryable discovery failure %s, never upstream messages', async status => {
  vi.mocked(getNativeModelCatalog).mockRejectedValueOnce(new ApiError('private upstream credential', status, null))
  await render(); await click('Cambiar modelo')
  expect(host.querySelector('[role=alert]')).not.toBeNull()
  expect(host.textContent).not.toContain('private upstream credential')
  expect(host.querySelector('select')).toBeNull()
  await click('Reintentar'); expect(host.querySelector('select')).not.toBeNull()
  expect(selectNativeModel).not.toHaveBeenCalled()
})

it('keeps saving single-flight and requires re-reading after an uncertain write', async () => {
  let reject!: (error: Error) => void
  vi.mocked(selectNativeModel).mockReturnValue(new Promise((_resolve, no) => { reject = no }))
  await render(); await click('Cambiar modelo'); await choose('account-terra')
  await act(async () => { button('Usar este modelo').click(); button('Usar este modelo').click() })
  expect(selectNativeModel).toHaveBeenCalledOnce()
  expect((button('Cancelar') as HTMLButtonElement).disabled).toBe(true)
  await act(async () => { reject(new Error('private write failure')) })
  expect(host.textContent).toContain('No se pudo confirmar el cambio')
  expect(host.textContent).not.toContain('private write failure')
  expect(host.querySelector('select')).toBeNull()
  await click('Consultar de nuevo'); expect(getNativeModelCatalog).toHaveBeenCalledTimes(2)
  expect(changed).not.toHaveBeenCalled()
})

it('can cancel a discovery, restores focus and ignores its late response', async () => {
  let resolve!: (value: typeof catalog) => void
  vi.mocked(getNativeModelCatalog).mockReturnValue(new Promise(yes => { resolve = yes }))
  await render(); await click('Cambiar modelo'); await click('Cancelar')
  expect(document.activeElement).toBe(button('Cambiar modelo'))
  await act(async () => { resolve(catalog) })
  expect(host.querySelector('select')).toBeNull()
  expect(selectNativeModel).not.toHaveBeenCalled()
})

it.each(['managed', 'inactive', 'other-provider', 'forbidden'])('does not offer local selection for %s', async kind => {
  if (kind === 'managed') provider.managed_by = 'cloud'
  if (kind === 'inactive') provider.is_active = false
  if (kind === 'other-provider') provider.provider_id = 'custom'
  if (kind === 'forbidden') access.allowed = false
  await render(); expect(host.textContent).toBe(''); expect(getNativeModelCatalog).not.toHaveBeenCalled()
})

it('uses English labels when selected', async () => {
  localStorage.setItem('safent_ui_locale', 'en')
  await render(); await click('Change model')
  expect(host.textContent).toContain('without signing in again')
})
