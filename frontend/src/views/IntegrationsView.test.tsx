import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getComposioStatus: vi.fn(), listComposioConnected: vi.fn(), listComposioApps: vi.fn(), getWebSearchStatus: vi.fn(), setupComposioMeta: vi.fn() }))
const session = vi.hoisted(() => ({
  current: { kind: 'authenticated' } as { kind: 'authenticated' } | { kind: 'unauthenticated'; reason: 'no_token' },
  listeners: new Set<() => void>(),
}))
vi.mock('../lib/token', async () => ({
  ...await vi.importActual('../lib/token'),
  getAuthStatus: () => session.current,
  subscribeAuthStatus: (listener: () => void) => { session.listeners.add(listener); return () => session.listeners.delete(listener) },
}))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('../api/crm', () => ({ listCrmConnections: vi.fn().mockResolvedValue({ context: 'a'.repeat(64), connections: [], limit: 100 }) }))
import IntegrationsView from './IntegrationsView'
import { ApiError } from '../api/client'
import { I18nProvider } from '../lib/i18n'
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  api.getComposioStatus.mockReset().mockResolvedValue({ has_key: true, entity_id: 'test' })
  api.listComposioConnected.mockReset().mockResolvedValue([])
  api.listComposioApps.mockReset().mockResolvedValue([{ slug: 'drive', name: 'Drive' }])
  api.getWebSearchStatus.mockReset().mockResolvedValue({ brave: true })
  api.setupComposioMeta.mockReset().mockResolvedValue({ ready: true })
  session.current = { kind: 'authenticated' }
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear() })
async function render() { await act(async () => { root.render(<MemoryRouter><IntegrationsView /></MemoryRouter>) }) }

function account(id: string, toolkit_slug: string, status = 'ACTIVE') {
  return { id, toolkit_slug, entity_id: 'test', status, auth_config_id: '' }
}

it('does not show empty/connected claims or connect actions when connection lookup failed', async () => {
  api.listComposioConnected.mockRejectedValue(new Error('offline'))
  await render()
  expect(container.querySelector('[role="alert"]')).not.toBeNull()
  expect(container.textContent).not.toContain('Sin apps conectadas todavía')
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('No se pudieron verificar tus conexiones')
  expect(container.querySelector('button[aria-label="Conectar Drive"]')?.hasAttribute('disabled')).toBe(true)
})

it('catalog failure preserves connected list, shows retry, and never claims everything connected', async () => {
  api.listComposioConnected.mockResolvedValue([account('ca_drive', 'drive')])
  api.listComposioApps.mockRejectedValue(new Error('offline'))
  await render()
  expect(container.textContent).toContain('Drive')
  expect(container.textContent).not.toContain('Todo conectado')
  expect(container.querySelector('[role="alert"]')).not.toBeNull()
})

it('latest focus refresh wins over delayed earlier lookup', async () => {
  let resolve!: (value: unknown) => void
  api.getComposioStatus.mockReturnValueOnce(new Promise(r => { resolve = r }))
  await render()
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  expect(container.textContent).toContain('Drive')
  await act(async () => { resolve({ has_key: false }) })
  expect(container.textContent).toContain('Drive')
})

it('invalid catalog shape is a recoverable error, not an empty catalog', async () => {
  api.listComposioApps.mockResolvedValue(null)
  await render()
  expect(container.querySelector('[role="alert"]')).not.toBeNull()
  expect(container.textContent).not.toContain('Todo conectado')
})

it('does not claim a fallback is active without explicit backend capability', async () => {
  api.getWebSearchStatus.mockResolvedValue({ brave:false, ddgs_fallback:false })
  await render()
  expect(container.textContent).toContain('Brave no está configurado')
  expect(container.textContent).not.toContain('Activo: DuckDuckGo')
})

it('renders the actual connected-account REST shape using catalog names and enables unrelated connections', async () => {
  api.listComposioConnected.mockResolvedValue([account('ca_mail', 'gmail')])
  api.listComposioApps.mockResolvedValue([{ slug: 'gmail', name: 'Correo de Google' }, { slug: 'drive', name: 'Drive' }])
  await render()
  const connected = container.querySelector('section[aria-label="Apps conectadas"]')!
  expect(connected.textContent).toContain('Correo de Google')
  expect(connected.textContent).toContain('Conectado')
  expect(connected.querySelector('[role="alert"]')).toBeNull()
  expect(container.querySelector('button[aria-label="Conectar Drive"]')?.hasAttribute('disabled')).toBe(false)
  expect(container.querySelector('button[aria-label="Conectar Correo de Google"]')).toBeNull()
})

it('keeps two accounts for one platform distinct and retains their real IDs', async () => {
  api.listComposioConnected.mockResolvedValue([account('ca_first', 'gmail'), account('ca_second', 'gmail')])
  await render()
  const rows = container.querySelectorAll('section[aria-label="Apps conectadas"] li')
  expect(rows).toHaveLength(2)
  expect(rows[0].textContent).toContain('Gmail')
  expect(rows[0].textContent).toContain('Conexión ca_first')
  expect(rows[1].textContent).toContain('Conexión ca_second')
})

it('uses readable fallback names when the catalog is unavailable', async () => {
  api.listComposioConnected.mockResolvedValue([account('ca_ads', 'googleads'), account('ca_custom', 'custom_tool')])
  api.listComposioApps.mockRejectedValue(new Error('offline'))
  await render()
  const connected = container.querySelector('section[aria-label="Apps conectadas"]')!
  expect(connected.textContent).toContain('Google Ads')
  expect(connected.textContent).toContain('Custom Tool')
  expect(connected.querySelector('a[aria-label="Gestionar Google Ads en Anuncios"]')?.getAttribute('href')).toBe('/anuncios')
})

it.each([
  ['INITIATED', 'Autorización pendiente'],
  ['EXPIRED', 'Autorización caducada'],
  ['FAILED', 'Error de conexión'],
  ['INACTIVE', 'Inactiva'],
  ['UNRECOGNIZED', 'Estado no disponible'],
])('does not label %s as connected or hide its catalog action', async (status, label) => {
  api.listComposioConnected.mockResolvedValue([account('ca_drive', 'drive', status)])
  await render()
  const connected = container.querySelector('section[aria-label="Apps conectadas"]')!
  expect(connected.textContent).toContain(label)
  expect(connected.textContent).not.toContain('Conectado')
  expect(container.querySelector('button[aria-label="Conectar Drive"]')?.hasAttribute('disabled')).toBe(false)
})

it('routes Google Ads and Meta Ads catalog entries to Anuncios without generic connection actions', async () => {
  api.listComposioConnected.mockResolvedValue([account('ca_gmail', 'gmail')])
  api.listComposioApps.mockResolvedValue([{ slug: 'googleads', name: 'Google Ads' }, { slug: 'metaads', name: 'Meta Ads' }])
  await render()
  const catalog = container.querySelector('section[aria-label="Apps disponibles"]')!
  for (const name of ['Google Ads', 'Meta Ads']) {
    expect(catalog.querySelector(`a[aria-label="Gestionar ${name} en Anuncios"]`)?.getAttribute('href')).toBe('/anuncios')
    expect(catalog.querySelector(`button[aria-label="Conectar ${name}"]`)).toBeNull()
  }
  expect(catalog.textContent).not.toContain('Conectado')
  expect(container.textContent).toContain('cada cuenta publicitaria necesita autorización')
})

it('keeps the Ads entry navigable when connections cannot be verified', async () => {
  api.listComposioConnected.mockRejectedValue(new Error('offline'))
  api.listComposioApps.mockResolvedValue([{ slug: 'metaads', name: 'Meta Ads' }])
  await render()
  const link = container.querySelector('a[aria-label="Gestionar Meta Ads en Anuncios"]')!
  expect(link.getAttribute('href')).toBe('/anuncios')
  expect(link.hasAttribute('aria-disabled')).toBe(false)
})

const META_CALLBACK = 'https://backend.composio.dev/api/v1/auth-apps/add'
const SYNTHETIC_SECRET = 'synthetic-meta-secret-only'
function metaForm() { return container.querySelector('form')! }
function metaInput(label: 'App ID' | 'App Secret') {
  const accessibleLabel = label === 'App ID' ? 'Identificador de la app (App ID)' : 'Clave de la app (App Secret)'
  return Array.from(metaForm().querySelectorAll('label')).find(node => node.textContent === accessibleLabel)!.querySelector('input')!
}
function fillMeta(id = '1234567890', secret = SYNTHETIC_SECRET) {
  metaInput('App ID').value = id
  metaInput('App Secret').value = secret
}
async function submitMeta() {
  await act(async () => { metaForm().dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
}
function setAuthenticated(authenticated: boolean) {
  session.current = authenticated ? { kind: 'authenticated' } : { kind: 'unauthenticated', reason: 'no_token' }
  for (const listener of session.listeners) listener()
}

it('shows labeled Meta preparation and its exact one-time callback only after Composio is available', async () => {
  await render()
  const form = metaForm()
  expect(form.getAttribute('aria-labelledby')).toBeTruthy()
  expect(form.textContent).toContain('quien administra la app de Meta debe añadir esta dirección de retorno')
  expect(form.textContent).toContain('La clave de la app (App Secret) se guarda en Composio, no en el navegador')
  expect(form.textContent).toContain('Copiar dirección')
  expect(form.textContent).not.toMatch(/callback/i)
  expect(form.textContent).toContain('no autoriza ninguna cuenta publicitaria')
  expect(form.querySelector('input[readonly]')?.getAttribute('value')).toBe(META_CALLBACK)
  expect(form.querySelector('a[target="_blank"]')?.getAttribute('href')).toBe('https://developers.facebook.com/apps/')
  expect(form.querySelector('a[target="_blank"]')?.getAttribute('rel')).toBe('noopener noreferrer')
  expect(metaInput('App Secret').type).toBe('password')
  expect(metaInput('App Secret').autocomplete).toBe('new-password')
  expect(metaInput('App ID').inputMode).toBe('numeric')
  expect(metaInput('App ID').required).toBe(true)
  expect(metaInput('App Secret').required).toBe(true)
})

it.each(['no-key', 'no-session'])('does not expose the Meta secret form with %s', async condition => {
  if (condition === 'no-key') api.getComposioStatus.mockResolvedValue({ has_key: false })
  else session.current = { kind: 'unauthenticated', reason: 'no_token' }
  await render()
  expect(container.textContent).not.toContain('Preparar Meta Ads en Composio')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('posts exactly the supplied Meta app credentials, clears the secret immediately, and never claims accounts are connected', async () => {
  let finish!: (result: { ready: true }) => void
  api.setupComposioMeta.mockReturnValue(new Promise(resolve => { finish = resolve }))
  await render()
  fillMeta(' 1234567890 ', ` ${SYNTHETIC_SECRET} `)
  await submitMeta()
  expect(api.setupComposioMeta).toHaveBeenCalledWith({ client_id: '1234567890', client_secret: SYNTHETIC_SECRET })
  expect(metaInput('App Secret').value).toBe('')
  expect(metaInput('App ID').disabled).toBe(true)
  expect(metaForm().querySelector('button[type="submit"]')?.hasAttribute('disabled')).toBe(true)
  expect(metaForm().textContent).toContain('Preparando Meta…')
  await submitMeta()
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(1)
  await act(async () => { finish({ ready: true }) })
  expect(metaForm().querySelector('[role="status"]')?.textContent).toContain('Meta está preparada en Composio')
  expect(metaForm().textContent).toContain('Todavía debes autorizar cada cuenta publicitaria en Anuncios')
  expect(metaForm().querySelector('a[href="/anuncios"]')).not.toBeNull()
  expect(metaInput('App Secret').value).toBe('')
  expect(metaForm().textContent).not.toContain('Conectado')
  expect(container.outerHTML).not.toContain(SYNTHETIC_SECRET)
  expect(localStorage.length).toBe(0)
  expect(sessionStorage.length).toBe(0)
})

it.each([
  ['', SYNTHETIC_SECRET], ['1234', SYNTHETIC_SECRET], ['1'.repeat(31), SYNTHETIC_SECRET],
  ['１２３４５', SYNTHETIC_SECRET], ['1234x', SYNTHETIC_SECRET], ['123456', ''], ['123456', 'x'.repeat(4097)],
])('rejects invalid Meta credentials before sending (case %#)', async (id, secret) => {
  await render()
  fillMeta(id, secret)
  await submitMeta()
  expect(metaForm().querySelector('[role="alert"]')?.textContent).toContain('Introduce el identificador numérico de la app (de 5 a 30 dígitos)')
  expect(metaInput('App ID').getAttribute('aria-invalid')).toBe('true')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it.each([
  [400, 'No se pudo preparar Meta'], [401, 'Tu sesión ha caducado'],
  [403, 'Sólo el propietario'], [429, 'Hay demasiados intentos'], [502, 'No se pudo preparar Meta'], [0, 'No se pudo preparar Meta'],
])('handles HTTP %s with a safe visible message and clears the secret', async (status, message) => {
  api.setupComposioMeta.mockRejectedValue(new ApiError(SYNTHETIC_SECRET, status, { client_secret: SYNTHETIC_SECRET }))
  await render()
  fillMeta()
  await submitMeta()
  expect(metaForm().querySelector('[role="alert"]')?.textContent).toContain(message)
  expect(container.outerHTML).not.toContain(SYNTHETIC_SECRET)
  expect(metaInput('App Secret').value).toBe('')
  expect(metaForm().querySelector('button[type="submit"]')?.hasAttribute('disabled')).toBe(false)
  expect(metaForm().textContent).not.toContain('Meta está preparada')
  expect(localStorage.length).toBe(0)
  expect(sessionStorage.length).toBe(0)
})

it('requires the secret again after failure and allows an explicit retry', async () => {
  api.setupComposioMeta.mockRejectedValueOnce(new Error(SYNTHETIC_SECRET)).mockResolvedValueOnce({ ready: true })
  await render()
  fillMeta()
  await submitMeta()
  await submitMeta()
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(1)
  fillMeta()
  await submitMeta()
  expect(api.setupComposioMeta).toHaveBeenCalledTimes(2)
  expect(metaForm().textContent).toContain('Meta está preparada en Composio')
})

it('copies only the public callback and confirms it accessibly', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  await render()
  fillMeta()
  await act(async () => { (metaForm().querySelector('button[type="button"]') as HTMLButtonElement).click() })
  expect(writeText).toHaveBeenCalledWith(META_CALLBACK)
  expect(metaForm().querySelector('[role="status"]')?.textContent).toBe('Dirección copiada.')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('falls back to selecting the callback when clipboard access fails', async () => {
  vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
  await render()
  await act(async () => { (metaForm().querySelector('button[type="button"]') as HTMLButtonElement).click() })
  const callback = metaForm().querySelector('input[readonly]') as HTMLInputElement
  expect(document.activeElement).toBe(callback)
  expect(callback.selectionStart).toBe(0)
  expect(callback.selectionEnd).toBe(META_CALLBACK.length)
  expect(metaForm().querySelector('[role="status"]')?.textContent).toContain('cópiala manualmente')
})

it('clears the secret on session loss and ignores a late preparation result from the old form', async () => {
  let finish!: (result: { ready: true }) => void
  api.setupComposioMeta.mockReturnValue(new Promise(resolve => { finish = resolve }))
  await render()
  fillMeta()
  await submitMeta()
  await act(async () => { setAuthenticated(false) })
  expect(container.querySelector('form')).toBeNull()
  await act(async () => { setAuthenticated(true) })
  expect(metaInput('App Secret').value).toBe('')
  await act(async () => { finish({ ready: true }) })
  expect(metaForm().textContent).not.toContain('Meta está preparada')
})

it('does not send credentials when session loss precedes the next render', async () => {
  await render()
  fillMeta()
  session.current = { kind: 'unauthenticated', reason: 'no_token' }
  await submitMeta()
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})

it('uses local English Meta setup copy without modifying the shared translations', async () => {
  localStorage.setItem('safent_ui_locale', 'en')
  await act(async () => { root.render(<I18nProvider><MemoryRouter><IntegrationsView /></MemoryRouter></I18nProvider>) })
  expect(metaForm().textContent).toContain('Prepare Meta Ads in Composio')
  expect(metaForm().textContent).toContain('The App Secret is stored in Composio, not in the browser')
})

it('keeps one Meta form visible during Composio refresh without resending its draft', async () => {
  await render()
  fillMeta()
  const form = metaForm()
  let finish!: (status: { has_key: boolean }) => void
  api.getComposioStatus.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  expect(metaForm()).toBe(form)
  expect(container.querySelectorAll('form')).toHaveLength(1)
  expect(metaInput('App Secret').value).toBe(SYNTHETIC_SECRET)
  await act(async () => { finish({ has_key: true }) })
  expect(metaForm()).toBe(form)
  expect(container.querySelectorAll('form')).toHaveLength(1)
  expect(metaInput('App ID').value).toBe('1234567890')
  expect(api.setupComposioMeta).not.toHaveBeenCalled()
})
