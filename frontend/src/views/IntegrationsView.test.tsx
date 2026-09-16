import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getComposioStatus: vi.fn(), listComposioConnected: vi.fn(), listComposioApps: vi.fn(), getWebSearchStatus: vi.fn(),
  getImageGenerationStatus: vi.fn(), setImageGenerationKey: vi.fn(), deleteImageGenerationKey: vi.fn(),
}))
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
import { I18nProvider } from '../lib/i18n'
vi.mock('./AdsSetupGuide', () => ({ default: ({ provider }: { provider: string }) => <div data-guide={provider}>Guide</div> }))
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  api.getComposioStatus.mockReset().mockResolvedValue({ has_key: true, entity_id: 'test' })
  api.listComposioConnected.mockReset().mockResolvedValue([])
  api.listComposioApps.mockReset().mockResolvedValue([{ slug: 'drive', name: 'Drive' }])
  api.getWebSearchStatus.mockReset().mockResolvedValue({ brave: true })
  api.getImageGenerationStatus.mockReset().mockResolvedValue({ provider: 'fal', has_key: false, model: null })
  api.setImageGenerationKey.mockReset().mockResolvedValue({ has_key: true })
  api.deleteImageGenerationKey.mockReset().mockResolvedValue({ has_key: false })
  session.current = { kind: 'authenticated' }
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear() })
async function render(entry = '/') { await act(async () => { root.render(<MemoryRouter initialEntries={[entry]}><IntegrationsView /></MemoryRouter>) }) }

function account(id: string, toolkit_slug: string, status = 'ACTIVE') {
  return { id, toolkit_slug, entity_id: 'test', status, auth_config_id: '' }
}

function clickButton(root: ParentNode, matcher: (text: string) => boolean) {
  const button = Array.from(root.querySelectorAll('button')).find(b => matcher(b.textContent ?? ''))
  if (!button) throw new Error('button not found')
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}

function typeInto(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value',
  )!.set!
  act(() => {
    nativeSetter.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

it.each(['google', 'meta'])('opens only the selected %s guide without fetching the unrelated catalog', async provider => {
  await render(`/capacidades?tab=integraciones&ads_setup=${provider}`)
  expect(container.querySelector('[data-guide]')?.getAttribute('data-guide')).toBe(provider)
  expect(api.getComposioStatus).not.toHaveBeenCalled()
  expect(api.getWebSearchStatus).not.toHaveBeenCalled()
})

it.each(['', '&ads_setup=unknown', '&ads_setup=meta&ads_setup=google'])('keeps services and both guide links visible for the catalog query %s', async query => {
  await render(`/capacidades?tab=integraciones${query}`)
  expect(container.querySelector('[data-guide]')).toBeNull()
  const links = Array.from(container.querySelectorAll('a'), node => node.getAttribute('href'))
  expect(links).toContain('/capacidades?tab=integraciones&ads_setup=meta')
  expect(links).toContain('/capacidades?tab=integraciones&ads_setup=google')
  expect(container.textContent).toContain('Drive')
  expect(container.querySelector('input[autocomplete="new-password"][required]')).toBeNull()
})

it('offers guide links even when Composio has no key', async () => {
  api.getComposioStatus.mockResolvedValue({ has_key: false })
  await render()
  expect(Array.from(container.querySelectorAll('a'), node => node.getAttribute('href'))).toContain('/capacidades?tab=integraciones&ads_setup=meta')
})

it('labels the guide links in English', async () => {
  localStorage.setItem('safent_ui_locale', 'en')
  await act(async () => { root.render(<I18nProvider><MemoryRouter><IntegrationsView /></MemoryRouter></I18nProvider>) })
  expect(container.textContent).toContain('Set up Meta Ads step by step')
})

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

// ── Image generation (FAL.ai) card ──────────────────────────────────────────────

it('shows "Sin configurar" and no remove button when no FAL.ai key is stored', async () => {
  await render()
  const section = container.querySelector('section[aria-label="Generación de imágenes"]')!
  expect(section.textContent).toContain('Sin configurar')
  expect(Array.from(section.querySelectorAll('button')).some(b => b.textContent?.includes('Quitar'))).toBe(false)
})

it('shows "Configurada" and a remove button when a FAL.ai key is stored', async () => {
  api.getImageGenerationStatus.mockResolvedValue({ provider: 'fal', has_key: true, model: 'fal-ai/flux-2/klein/9b' })
  await render()
  const section = container.querySelector('section[aria-label="Generación de imágenes"]')!
  expect(section.textContent).toContain('Configurada')
  expect(Array.from(section.querySelectorAll('button')).some(b => b.textContent?.includes('Quitar'))).toBe(true)
})

it('saving a FAL.ai key calls the API and refreshes to the configured state', async () => {
  await render()
  const section = container.querySelector('section[aria-label="Generación de imágenes"]')!
  const input = section.querySelector('input[type="password"]') as HTMLInputElement
  typeInto(input, 'fal-secret-key')
  api.getImageGenerationStatus.mockResolvedValue({ provider: 'fal', has_key: true, model: null })
  clickButton(section, text => text.includes('Guardar'))
  await flush()
  expect(api.setImageGenerationKey).toHaveBeenCalledWith('fal-secret-key')
  expect(container.querySelector('section[aria-label="Generación de imágenes"]')!.textContent).toContain('Configurada')
})

it('does not call the API when saving an empty FAL.ai key', async () => {
  await render()
  const section = container.querySelector('section[aria-label="Generación de imágenes"]')!
  clickButton(section, text => text.includes('Guardar'))
  await flush()
  expect(api.setImageGenerationKey).not.toHaveBeenCalled()
})

it('removing the FAL.ai key calls the API and refreshes to the unconfigured state', async () => {
  api.getImageGenerationStatus.mockResolvedValue({ provider: 'fal', has_key: true, model: null })
  await render()
  const section = container.querySelector('section[aria-label="Generación de imágenes"]')!
  api.getImageGenerationStatus.mockResolvedValue({ provider: 'fal', has_key: false, model: null })
  clickButton(section, text => text.includes('Quitar'))
  await flush()
  expect(api.deleteImageGenerationKey).toHaveBeenCalled()
  expect(container.querySelector('section[aria-label="Generación de imágenes"]')!.textContent).toContain('Sin configurar')
})

it('labels the image generation card in English', async () => {
  localStorage.setItem('safent_ui_locale', 'en')
  await act(async () => { root.render(<I18nProvider><MemoryRouter><IntegrationsView /></MemoryRouter></I18nProvider>) })
  await flush()
  expect(container.textContent).toContain('Image generation')
  expect(container.textContent).toContain('Not configured')
})
