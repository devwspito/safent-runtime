import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getComposioStatus: vi.fn(), listComposioConnected: vi.fn(), listComposioApps: vi.fn(), getWebSearchStatus: vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
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
async function render() { await act(async () => { root.render(<IntegrationsView />) }) }

it('does not show empty/connected claims or connect actions when connection lookup failed', async () => {
  api.listComposioConnected.mockRejectedValue(new Error('offline'))
  await render()
  expect(container.querySelector('[role="alert"]')).not.toBeNull()
  expect(container.textContent).not.toContain('Aún no has conectado')
  expect(container.querySelector('button[aria-label="Conectar Drive"]')?.hasAttribute('disabled')).toBe(true)
})

it('catalog failure preserves connected list, shows retry, and never claims everything connected', async () => {
  api.listComposioConnected.mockResolvedValue([{ slug: 'drive', name: 'Drive' }])
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
