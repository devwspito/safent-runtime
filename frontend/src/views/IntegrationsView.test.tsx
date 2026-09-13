import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getComposioStatus: vi.fn(), listComposioConnected: vi.fn(), listComposioApps: vi.fn(), getWebSearchStatus: vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('../api/crm', () => ({ listCrmConnections: vi.fn().mockResolvedValue({ context: 'a'.repeat(64), connections: [], limit: 100 }) }))
import IntegrationsView from './IntegrationsView'
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  api.getComposioStatus.mockReset().mockResolvedValue({ has_key: true, entity_id: 'test' })
  api.listComposioConnected.mockReset().mockResolvedValue([])
  api.listComposioApps.mockReset().mockResolvedValue([{ slug: 'drive', name: 'Drive' }])
  api.getWebSearchStatus.mockReset().mockResolvedValue({ brave: true })
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals() })
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
