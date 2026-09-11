import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ContextPanel from './ContextPanel'
vi.mock('../lib/token', () => ({ token: () => 'test-token', getAuthStatus: () => ({ kind: 'authenticated' }), refreshToken: vi.fn() }))

let host: HTMLDivElement
let root: Root
let filesFail = false
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  filesFail = false
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/workspace/files')) return new Response(JSON.stringify(filesFail ? { detail: 'unavailable' } : [{ name: 'Brand manual.pdf', path: '/Brand manual.pdf', size: 12 }, { name: 'Brand', path: '/Brand', size: 0, is_dir: true }]), { status: filesFail ? 503 : 200 })
    if (url.includes('/integrations/composio/connected')) return new Response('[]')
    if (url.endsWith('/skills')) return new Response(JSON.stringify([{ skill_id: 'brief', name: 'Brand brief' }]))
    return new Response('{}')
  }))
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })
async function render(key = 'a', onClose = vi.fn()) { await act(async () => { root.render(<ContextPanel key={key} onClose={onClose} />) }) }
function button(label: string) { return host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)! }

it('keeps source failures independent and never invents web search', async () => {
  filesFail = true
  await render()
  expect(host.textContent).toContain('No se pudo consultar esta fuente')
  expect(host.textContent).toContain('Brand brief')
  expect(host.textContent).not.toContain('Búsqueda web')
  expect(host.textContent).not.toContain('Sin archivos todavía')
  filesFail = false
  await act(async () => { button('Reintentar Carpeta de trabajo').click() })
  expect(host.textContent).toContain('Brand manual.pdf')
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/skills'))).toHaveLength(1)
})

it('retains a visibly unverified previous list after refresh fails; directories are not downloads', async () => {
  await render()
  expect(host.querySelector('a[download="Brand"]')).toBeNull()
  expect(host.querySelector('a[download="Brand manual.pdf"]')?.getAttribute('href')).toContain('%2FBrand%20manual.pdf')
  filesFail = true
  await act(async () => { button('Actualizar fuentes de contexto').click() })
  expect(host.textContent).toContain('Brand manual.pdf')
  expect(host.textContent).toContain('sin verificar')
})

it('returns focus to its opener and handles Escape only inside the panel', async () => {
  const opener = document.createElement('button'); document.body.append(opener); opener.focus()
  const close = vi.fn()
  await render('a', close)
  expect(host.contains(document.activeElement)).toBe(true)
  act(() => { document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
  expect(close).toHaveBeenCalledOnce()
  act(() => { root.render(null) })
  expect(document.activeElement).toBe(opener)
  opener.remove()
})

it('rejects malformed connector responses without presenting them as an empty source', async () => {
  const initialFetch = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (input, init) => String(input).includes('/integrations/composio/connected')
    ? new Response(JSON.stringify({ apps: [] })) : initialFetch(input, init))
  await render()
  expect(host.textContent).toContain('No se pudo consultar esta fuente')
  expect(host.textContent).not.toContain('Sin conexiones activas')
  expect(host.textContent).toContain('Brand manual.pdf')
})
