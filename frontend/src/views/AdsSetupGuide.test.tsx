import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getComposioStatus: vi.fn(), getComposioAdsConfig: vi.fn(), prepareComposioAds: vi.fn(), setComposioApiKey: vi.fn(), setupComposioMeta: vi.fn() }))
const external = vi.hoisted(() => vi.fn())
const session = vi.hoisted(() => ({
  current: { kind: 'authenticated' } as { kind: 'authenticated' } | { kind: 'unauthenticated'; reason: 'no_token' },
  scope: 'owner-test-only', listeners: new Set<() => void>(),
}))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('../lib/token', async () => ({ ...await vi.importActual('../lib/token'), token: () => session.scope,
  getAuthStatus: () => session.current, subscribeAuthStatus: (listener: () => void) => { session.listeners.add(listener); return () => session.listeners.delete(listener) },
}))
vi.mock('../lib/adsSetupLinks', () => ({ openAdsSetupLink: external }))
import { ApiError } from '../api/client'
import { I18nProvider } from '../lib/i18n'
import AdsSetupGuide from './AdsSetupGuide'

const SECRET = 'synthetic-app-secret-only'
const KEY = 'synthetic-composio-key-only'
const CALLBACK = 'https://backend.composio.dev/api/v1/auth-apps/add'
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  session.current = { kind: 'authenticated' }; session.scope = 'owner-test-only'
  api.getComposioStatus.mockReset().mockResolvedValue({ has_key: true })
  api.getComposioAdsConfig.mockReset().mockResolvedValue({ ready: false })
  api.prepareComposioAds.mockReset().mockResolvedValue({ googleads: true, metaads: false })
  api.setComposioApiKey.mockReset().mockResolvedValue({ has_key: true })
  api.setupComposioMeta.mockReset().mockResolvedValue({ ready: true })
  external.mockReset().mockResolvedValue(undefined)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear() })

async function render(provider: 'google' | 'meta' = 'meta', strict = false) {
  const content = <I18nProvider><MemoryRouter initialEntries={['/setup']}><Routes>
    <Route path="/setup" element={<AdsSetupGuide provider={provider} />} />
    <Route path="/capacidades" element={<p>Catalog destination</p>} />
    <Route path="/anuncios" element={<p>Ads destination</p>} />
  </Routes></MemoryRouter></I18nProvider>
  await act(async () => { root.render(strict ? <StrictMode>{content}</StrictMode> : content) })
}
function field(label: string) {
  const node = Array.from(container.querySelectorAll('label')).find(item => item.textContent === label)!
  return container.querySelector<HTMLInputElement>(`[id="${node.htmlFor}"]`)!
}
function button(label: string) { return Array.from(container.querySelectorAll('button')).find(node => node.textContent === label)! }
async function click(node: HTMLElement) { await act(async () => { node.click() }) }
async function change(input: HTMLInputElement, value: string) {
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })) })
}
async function fillMeta(id = '1234567890', secret = SECRET, confirm = true) {
  await change(field('Identificador de la app (App ID)'), id)
  field('Clave de la app (App Secret)').value = secret
  const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  if (checkbox.checked !== confirm) await click(checkbox)
}
async function submit() { await act(async () => { container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) }) }
function deferred<T>() { let resolve!: (value: T) => void; return { promise: new Promise<T>(r => { resolve = r }), resolve: (value: T) => resolve(value) } }

it('shows the complete missing-Composio path before asking for Meta credentials and never posts automatically', async () => {
  api.getComposioStatus.mockResolvedValue({ has_key: false })
  await render()
  expect(container.textContent).toContain('Settings → API Keys')
  expect(container.textContent).toContain('Inicia sesión o crea tu cuenta')
  expect(field('Clave de Composio').type).toBe('password')
  expect(field('Clave de Composio').autocomplete).toBe('new-password')
  expect(api.getComposioAdsConfig).toHaveBeenCalledWith('metaads')
  expect(container.querySelector('input[inputmode="numeric"]')).toBeNull()
  expect(api.setComposioApiKey).not.toHaveBeenCalled()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
  await click(button('Abrir Composio'))
  expect(external).toHaveBeenCalledWith('https://dashboard.composio.dev/')
  expect(container.querySelector('input[readonly]')?.getAttribute('value')).toBe('https://dashboard.composio.dev/')
})

it('clears the key before explicit saving, prevents duplicate sends, and checks readiness again', async () => {
  const pending = deferred<{ has_key: true }>()
  api.getComposioStatus.mockResolvedValueOnce({ has_key: false }).mockResolvedValue({ has_key: true })
  api.setComposioApiKey.mockReturnValue(pending.promise)
  await render('google')
  const keyInput = field('Clave de Composio'); keyInput.value = ` ${KEY} `
  await submit(); await submit()
  expect(api.setComposioApiKey).toHaveBeenCalledExactlyOnceWith(KEY)
  expect(keyInput.value).toBe('')
  expect(button('Guardando…').disabled).toBe(true)
  await act(async () => { pending.resolve({ has_key: true }) })
  expect(api.getComposioStatus).toHaveBeenCalledTimes(2)
  expect(container.textContent).toContain('No necesitas crear un cliente OAuth')
  expect(api.prepareComposioAds).not.toHaveBeenCalled()
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0)
})

it('requires an entered key and keeps a failed key save safely retryable', async () => {
  api.getComposioStatus.mockResolvedValue({ has_key: false })
  api.setComposioApiKey.mockRejectedValueOnce(new ApiError(KEY, 409, { api_key: KEY }))
  await render(); await submit()
  expect(api.setComposioApiKey).not.toHaveBeenCalled()
  field('Clave de Composio').value = KEY
  await submit()
  expect(field('Clave de Composio').value).toBe('')
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('vuelve a pegarla')
  expect(container.outerHTML).not.toContain(KEY)
  await submit()
  expect(api.setComposioApiKey).toHaveBeenCalledTimes(1)
})

it.each(['google', 'meta'] as const)('does not repeat saved %s configuration or request secrets', async provider => {
  api.getComposioAdsConfig.mockResolvedValue({ ready: true })
  await render(provider)
  expect(api.getComposioAdsConfig).toHaveBeenCalledWith(`${provider}ads`)
  expect(container.textContent).toContain('La configuración está guardada')
  expect(container.textContent).toContain('no significa que una cuenta publicitaria esté conectada')
  expect(container.querySelector('input[type="password"]')).toBeNull()
  expect(api.prepareComposioAds).not.toHaveBeenCalled(); expect(api.setupComposioMeta).not.toHaveBeenCalled()
  const link = container.querySelector<HTMLAnchorElement>(`a[href="/anuncios?connect=${provider}"]`)!
  await click(link)
  expect(container.textContent).toContain('Ads destination')
})

it.each(['status', 'configuration'])('keeps %s service failures distinct from missing configuration and allows a read-only retry', async source => {
  const call = source === 'status' ? api.getComposioStatus : api.getComposioAdsConfig
  call.mockRejectedValueOnce(new ApiError(SECRET, 503, { secret: SECRET }))
  await render()
  expect(container.querySelector('form')).toBeNull()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('todavía no sabemos')
  expect(container.outerHTML).not.toContain(SECRET)
  expect(document.activeElement).toBe(container.querySelector('[role="alert"]'))
  await click(button('Volver a comprobar'))
  expect(field('Clave de la app (App Secret)').value).toBe('')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it.each([[401, 'Tu sesión ha caducado'], [403, 'Sólo el propietario']] as const)('owner read HTTP %s blocks all credential forms even without a key', async (status, message) => {
  api.getComposioStatus.mockResolvedValue({ has_key: false })
  api.getComposioAdsConfig.mockRejectedValue(new ApiError(SECRET, status, null))
  await render()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(message)
  expect(container.querySelector('form')).toBeNull()
  expect(api.setComposioApiKey).not.toHaveBeenCalled()
})

it('prepares Google only on request, distinguishes false readiness, then permits retry and authorization', async () => {
  api.prepareComposioAds.mockResolvedValueOnce({ googleads: false, metaads: true }).mockResolvedValueOnce({ googleads: true, metaads: true })
  await render('google')
  expect(container.querySelector('input')).toBeNull()
  expect(container.textContent).toContain('Gmail no concede por sí sola acceso publicitario')
  await submit()
  expect(api.prepareComposioAds).toHaveBeenCalledWith()
  expect(container.querySelector('[role="alert"]')).not.toBeNull()
  expect(container.querySelector('a[href="/anuncios?connect=google"]')).toBeNull()
  await submit()
  expect(container.querySelector('a[href="/anuncios?connect=google"]')).not.toBeNull()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('shows App ID and App Secret together above the exact callback, with direct user-derived links and human confirmation', async () => {
  await render()
  const app = field('Identificador de la app (App ID)')
  const secret = field('Clave de la app (App Secret)')
  const callback = field('Dirección de retorno')
  expect(app.compareDocumentPosition(secret) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(secret.compareDocumentPosition(callback) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(secret.closest('details')).toBeNull()
  expect(secret.type).toBe('password'); expect(secret.autocomplete).toBe('new-password')
  expect(callback.value).toBe(CALLBACK); expect(callback.readOnly).toBe(true)
  expect(container.textContent).toContain('no el número de una cuenta publicitaria')
  expect(container.textContent).toContain('Conecta Meta a través de Composio')
  expect(container.textContent).toContain('Sólo esta primera vez necesitas el App ID y el App Secret')
  expect(container.textContent).toContain('Safent no puede verificar esa pantalla de Meta')
  expect(container.textContent).toContain('contraseña, introdúcela sólo en Meta')
  expect(container.textContent).not.toContain('127.0.0.1')
  await change(app, '9876543210')
  await click(button('Abrir información básica'))
  expect(external).toHaveBeenLastCalledWith('https://developers.facebook.com/apps/9876543210/settings/basic/')
  await click(button('Abrir configuración de acceso'))
  expect(external).toHaveBeenLastCalledWith('https://developers.facebook.com/apps/9876543210/fb-login/settings/')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('posts only supplied Meta app credentials after confirmation, clears before dispatch, and single-flights', async () => {
  const pending = deferred<{ ready: true }>()
  api.setupComposioMeta.mockReturnValue(pending.promise)
  await render(); await fillMeta()
  const secret = field('Clave de la app (App Secret)')
  expect(secret.value).toBe(SECRET)
  api.setupComposioMeta.mockImplementation(() => { expect(secret.value).toBe(''); return pending.promise })
  await submit(); await submit()
  expect(api.setupComposioMeta).toHaveBeenCalledExactlyOnceWith({ client_id: '1234567890', client_secret: SECRET })
  expect(field('Identificador de la app (App ID)').disabled).toBe(true)
  expect(secret.disabled).toBe(true)
  await act(async () => { pending.resolve({ ready: true }) })
  expect(container.querySelector('a[href="/anuncios?connect=meta"]')).not.toBeNull()
  expect(container.querySelector('input[type="password"]')).toBeNull()
  expect(container.outerHTML).not.toContain(SECRET)
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0)
})

it.each(['', '1234', '1'.repeat(31), '１２３４５', '12345x'])('rejects invalid Meta App ID %s before posting', async id => {
  await render(); await fillMeta(id); await submit()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('de 5 a 30 dígitos')
})

it.each(['', 'x'.repeat(4097)])('rejects missing or oversized App Secret (case %#)', async secret => {
  await render(); await fillMeta('1234567890', secret); await submit()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('hasta 4096 caracteres')
})

it('does not prepare Meta without explicit human callback confirmation', async () => {
  await render(); await fillMeta('1234567890', SECRET, false); await submit()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Confirma que has guardado')
  expect(field('Clave de la app (App Secret)').value).toBe(SECRET)
})

it('requires a fresh callback confirmation when the App ID changes from A to B', async () => {
  await render(); await fillMeta('1234567890')
  const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  expect(checkbox.checked).toBe(true)
  await change(field('Identificador de la app (App ID)'), '9876543210')
  expect(checkbox.checked).toBe(false)
  await submit()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Confirma que has guardado')
  await click(checkbox); await submit()
  expect(api.setupComposioMeta).toHaveBeenCalledExactlyOnceWith({ client_id: '9876543210', client_secret: SECRET })
})

it.each([[400, 'No se pudo completar'], [401, 'Tu sesión ha caducado'], [403, 'Sólo el propietario'], [429, 'Hay demasiados intentos'], [503, 'No se pudo completar']] as const)('handles Meta HTTP %s safely and clears the secret', async (status, message) => {
  api.setupComposioMeta.mockRejectedValue(new ApiError(SECRET, status, { secret: SECRET }))
  await render(); await fillMeta()
  const secret = field('Clave de la app (App Secret)')
  await submit()
  expect(secret.value).toBe('')
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(message)
  expect(container.outerHTML).not.toContain(SECRET)
  expect(container.querySelector('a[href="/anuncios?connect=meta"]')).toBeNull()
  if (status === 401 || status === 403) expect(container.querySelector('form')).toBeNull()
})

it('retries Meta only after a fresh secret, preserving App ID and human confirmation', async () => {
  api.setupComposioMeta.mockRejectedValueOnce(new Error(SECRET)).mockResolvedValueOnce({ ready: true })
  await render(); await fillMeta(); await submit(); await submit()
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(1)
  expect(field('Identificador de la app (App ID)').value).toBe('1234567890')
  expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true)
  field('Clave de la app (App Secret)').value = SECRET
  await submit()
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(2)
  expect(container.querySelector('a[href="/anuncios?connect=meta"]')).not.toBeNull()
})

it('can reconcile a failed save by reading existing readiness without sending the secret again', async () => {
  api.setupComposioMeta.mockRejectedValueOnce(new Error('network failed after save'))
  await render(); await fillMeta(); await submit()
  api.getComposioAdsConfig.mockResolvedValue({ ready: true })
  await click(button('Volver a comprobar'))
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(1)
  expect(container.querySelector('a[href="/anuncios?connect=meta"]')).not.toBeNull()
  expect(container.querySelector('input[type="password"]')).toBeNull()
})

it('always offers public copy fallback, redacts opener errors, and selects the callback if copying fails', async () => {
  external.mockRejectedValue(new Error(SECRET))
  const writeText = vi.fn().mockRejectedValue(new Error(SECRET))
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  await render()
  expect(container.querySelectorAll('input[readonly]').length).toBeGreaterThan(1)
  await click(button('Abrir mis apps de Meta'))
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Copia esta dirección')
  expect(container.outerHTML).not.toContain(SECRET)
  const callback = field('Dirección de retorno')
  await click(callback.parentElement!.querySelector('button')!)
  expect(writeText).toHaveBeenCalledWith(CALLBACK)
  expect(document.activeElement).toBe(callback)
  expect(callback.selectionEnd).toBe(CALLBACK.length)
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('clears drafts on cancel and ignores a late preparation result', async () => {
  const pending = deferred<{ ready: true }>()
  api.setupComposioMeta.mockReturnValue(pending.promise)
  await render(); await fillMeta(); await submit()
  const cancel = Array.from(container.querySelectorAll('a')).find(node => node.textContent === 'Cancelar')!
  await click(cancel)
  await act(async () => { pending.resolve({ ready: true }) })
  expect(container.textContent).toBe('Catalog destination')
  expect(container.outerHTML).not.toContain(SECRET)
})

it('clears an unsent secret on navigation away without posting', async () => {
  await render(); await fillMeta()
  const secret = field('Clave de la app (App Secret)')
  await click(Array.from(container.querySelectorAll('a')).find(node => node.textContent === 'Volver a Integraciones')!)
  expect(secret.value).toBe('')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('gates an absent session and clears secrets immediately when the session is lost', async () => {
  session.current = { kind: 'unauthenticated', reason: 'no_token' }
  await render()
  expect(api.getComposioStatus).not.toHaveBeenCalled()
  await act(async () => { session.current = { kind: 'authenticated' }; session.listeners.forEach(listener => listener()) })
  await fillMeta()
  const secret = field('Clave de la app (App Secret)')
  await act(async () => { session.current = { kind: 'unauthenticated', reason: 'no_token' }; session.listeners.forEach(listener => listener()) })
  expect(secret.value).toBe(''); expect(container.querySelector('form')).toBeNull()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('will not submit or apply an old result after the owner credential scope changes', async () => {
  const pending = deferred<{ ready: true }>()
  api.setupComposioMeta.mockReturnValue(pending.promise)
  await render(); await fillMeta(); await submit()
  session.scope = 'different-owner'
  await act(async () => { pending.resolve({ ready: true }) })
  expect(container.querySelector('a[href="/anuncios?connect=meta"]')).toBeNull()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Tu sesión ha caducado')
  expect(container.querySelector('form')).toBeNull()
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(1)
})

it('resets drafts and ignores stale configuration after switching provider', async () => {
  const pending = deferred<{ ready: boolean }>()
  api.getComposioAdsConfig.mockReturnValueOnce(pending.promise).mockResolvedValue({ ready: false })
  await render('meta'); await render('google')
  await act(async () => { pending.resolve({ ready: true }) })
  expect(container.textContent).toContain('Preparar Google')
  expect(container.querySelector('a[href="/anuncios?connect=google"]')).toBeNull()
  expect(container.querySelector('input[type="password"]')).toBeNull()
})

it('loads correctly under StrictMode and keeps the guide in English', async () => {
  localStorage.setItem('safent_ui_locale', 'en')
  await render('meta', true)
  expect(field('App Secret').type).toBe('password')
  expect(container.textContent).toContain('App settings → Basic')
  expect(container.textContent).toContain('This checkbox is your confirmation')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})
