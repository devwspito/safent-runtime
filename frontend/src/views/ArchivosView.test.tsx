import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { WorkspaceFile } from '../api/types'

// No @testing-library in this project — render directly via react-dom
// (mirrors IntegrationsView.test.tsx / MemoriaView.test.tsx). The file
// drawer body renders through a Base UI portal appended to document.body
// (see Drawer.test.tsx), so drawer-scoped queries go through `document`.

const api = vi.hoisted(() => ({
  listWorkspaceFiles: vi.fn(),
  uploadWorkspaceFile: vi.fn(),
}))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))

const sileo = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }))
vi.mock('sileo', () => ({ sileo }))

import ArchivosView from './ArchivosView'

const FILE_A: WorkspaceFile = { name: 'a.txt', path: 'a.txt', size: 10, kind: 'text', is_dir: false, modified: '2026-01-01T00:00:00Z' }
const FILE_B: WorkspaceFile = { name: 'b.txt', path: 'b.txt', size: 12, kind: 'text', is_dir: false, modified: '2026-01-02T00:00:00Z' }
const DIR_X: WorkspaceFile = { name: 'x', path: 'x', size: 0, is_dir: true, kind: 'directory' }
const FILE_ROOT2: WorkspaceFile = { name: 'root-file.txt', path: 'root-file.txt', size: 5, kind: 'text', is_dir: false }
const STALE_ENTRY: WorkspaceFile = { name: 'stale-in-x.txt', path: 'x/stale-in-x.txt', size: 5, kind: 'text', is_dir: false }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  api.listWorkspaceFiles.mockReset().mockResolvedValue([FILE_A, FILE_B])
  api.uploadWorkspaceFile.mockReset()
  sileo.success.mockReset(); sileo.error.mockReset(); sileo.warning.mockReset()
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

async function render() { await act(async () => { root.render(<ArchivosView />) }) }

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

function clickButton(matcher: (text: string) => boolean, scope: ParentNode = document) {
  const button = Array.from(scope.querySelectorAll('button')).find(b => matcher(b.textContent ?? ''))
  if (!button) throw new Error('button not found')
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}

async function clickEntry(name: string, kindLabel: 'Archivo' | 'Carpeta') {
  const entry = container.querySelector<HTMLElement>(`[aria-label="${kindLabel}: ${name}"]`)
  if (!entry) throw new Error(`entry not found: ${name}`)
  act(() => { entry.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await flush()
}

it('an HTTP error while loading a preview is an explicit error, never rendered as file content', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve('<html>not found</html>') }))
  await render()

  await clickEntry('a.txt', 'Archivo')

  const errorBox = document.querySelector('[role="alert"]')
  expect(errorBox?.textContent).toContain('No se pudo cargar la vista previa de este archivo.')
  expect(document.body.textContent).not.toContain('not found')
  expect(document.querySelector('pre')).toBeNull()
})

it('a late preview response for a previously-selected file cannot overwrite the file now open in the drawer', async () => {
  const pendingA = deferred<{ ok: boolean; text: () => Promise<string> }>()
  const fetchMock = vi.fn((url: string) => {
    if (url.includes('path=a.txt')) return pendingA.promise
    if (url.includes('path=b.txt')) return Promise.resolve({ ok: true, text: () => Promise.resolve('FULL B CONTENT') })
    return Promise.reject(new Error(`unexpected url: ${url}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  await render()

  await clickEntry('a.txt', 'Archivo') // preview fetch left pending
  await clickEntry('b.txt', 'Archivo') // switch before A resolves — resolves immediately

  expect(document.querySelector('pre')?.textContent).toBe('FULL B CONTENT')

  await act(async () => { pendingA.resolve({ ok: true, text: () => Promise.resolve('STALE A CONTENT') }) })

  // A's late response must be silently discarded: still showing B's
  // preview, no stale content leak and no spurious error surfaced.
  expect(document.querySelector('pre')?.textContent).toBe('FULL B CONTENT')
  expect(document.querySelector('[role="alert"]')).toBeNull()
})

it('an older folder listing cannot clobber the folder the user has since navigated back to', async () => {
  const pendingX = deferred<WorkspaceFile[]>()
  api.listWorkspaceFiles
    .mockReset()
    .mockResolvedValueOnce([DIR_X])       // initial root listing
    .mockReturnValueOnce(pendingX.promise) // entering x/ — left pending
    .mockResolvedValueOnce([FILE_ROOT2])  // navigating back to root — resolves fast
  await render()

  await clickEntry('x', 'Carpeta')
  clickButton(t => t === 'Workspace') // breadcrumb back to root, while x/ is still loading
  await flush()

  expect(container.textContent).toContain('root-file.txt')
  expect(container.textContent).not.toContain('stale-in-x.txt')

  await act(async () => { pendingX.resolve([STALE_ENTRY]) })

  expect(container.textContent).toContain('root-file.txt')
  expect(container.textContent).not.toContain('stale-in-x.txt')
})
