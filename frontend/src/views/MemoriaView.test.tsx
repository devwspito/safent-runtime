import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MemoryEntryDetail, MemoryItem } from '../api/types'

// No @testing-library in this project — render directly via react-dom
// (mirrors IntegrationsView.test.tsx / SkillsView.test.tsx). The drawer body
// renders through a Base UI portal appended to document.body (see
// Drawer.test.tsx), so drawer-scoped queries go through `document`, not the
// mount `container`.

const api = vi.hoisted(() => ({
  listMemory: vi.fn(),
  searchMemory: vi.fn(),
  getMemoryEntry: vi.fn(),
  updateMemoryEntry: vi.fn(),
  forgetMemoryItem: vi.fn(),
}))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))

const sileo = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }))
vi.mock('sileo', () => ({ sileo }))

import MemoriaView from './MemoriaView'

const ITEM_A: MemoryItem = { id: 'agent:0', content_truncated: 'A truncated', target: 'agent', created_at: '2026-01-01T00:00:00Z' }
const ITEM_B: MemoryItem = { id: 'agent:1', content_truncated: 'B truncated', target: 'agent', created_at: '2026-01-02T00:00:00Z' }

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
  api.listMemory.mockReset().mockResolvedValue([ITEM_A, ITEM_B])
  api.searchMemory.mockReset()
  api.getMemoryEntry.mockReset()
  api.updateMemoryEntry.mockReset()
  api.forgetMemoryItem.mockReset()
  sileo.success.mockReset(); sileo.error.mockReset(); sileo.warning.mockReset()
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals() })

async function render() { await act(async () => { root.render(<MemoriaView />) }) }

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

function rows(): HTMLElement[] {
  return Array.from(container.querySelectorAll('.memory-item'))
}

async function clickRow(i: number) {
  act(() => { rows()[i].dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await flush()
}

function clickButton(matcher: (text: string) => boolean, scope: ParentNode = document) {
  const button = Array.from(scope.querySelectorAll('button')).find(b => matcher(b.textContent ?? ''))
  if (!button) throw new Error('button not found')
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}

function typeInto(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    nativeSetter.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function typeIntoTextarea(textarea: HTMLTextAreaElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
  act(() => {
    nativeSetter.call(textarea, value)
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function editor(): HTMLTextAreaElement | null {
  return document.querySelector<HTMLTextAreaElement>('#memory-edit')
}

function saveButton(): HTMLButtonElement | null {
  return document.querySelector<HTMLButtonElement>('button[aria-label="Guardar cambios en esta entrada de memoria"]')
}

it('a late detail response for a previously-open item cannot overwrite the item now open in the drawer', async () => {
  const pendingA = deferred<MemoryEntryDetail>()
  api.getMemoryEntry.mockImplementation((id: string) => {
    if (id === 'agent:0') return pendingA.promise
    if (id === 'agent:1') return Promise.resolve({ id: 'agent:1', target: 'agent', content: 'FULL B', entry_index: 1 })
    return Promise.reject(new Error('unexpected id'))
  })
  await render()

  await clickRow(0) // open A — fetch left pending
  await clickRow(1) // open B before A resolves — fetch resolves immediately

  expect(editor()?.value).toBe('FULL B')

  await act(async () => { pendingA.resolve({ id: 'agent:0', target: 'agent', content: 'FULL A', entry_index: 0 }) })

  // A's late response must be silently discarded: still showing B, no
  // cross-item content leak and no spurious error surfaced.
  expect(editor()?.value).toBe('FULL B')
  expect(document.querySelector('[role="alert"]')).toBeNull()
})

it('a failed detail load is an explicit error, not an editable truncated preview', async () => {
  api.getMemoryEntry.mockRejectedValueOnce(new Error('boom'))
  await render()

  await clickRow(0)

  const alert = document.querySelector('[role="alert"]')
  expect(alert?.textContent).toContain('No se pudo cargar el contenido completo')
  expect(editor()).toBeNull()
  expect(saveButton()?.disabled).toBe(true)

  // Retry re-fetches and, on success, the entry becomes editable.
  api.getMemoryEntry.mockResolvedValueOnce({ id: 'agent:0', target: 'agent', content: 'FULL A', entry_index: 0 })
  clickButton(t => t.includes('Reintentar'), alert!)
  await flush()

  expect(editor()?.value).toBe('FULL A')
  expect(document.querySelector('[role="alert"]')).toBeNull()
})

it('an older recent-list response cannot clobber a newer search result', async () => {
  const pendingRecent = deferred<MemoryItem[]>()
  api.listMemory.mockReturnValueOnce(pendingRecent.promise)
  api.searchMemory.mockResolvedValue([ITEM_B])
  await render() // triggers the initial recent-list load, left pending

  const input = container.querySelector<HTMLInputElement>('#memory-search')!
  typeInto(input, 'truncated')
  // Enter fires the search directly, unlike the Search button (disabled
  // while the initial recent-list request is still loading) — this is the
  // realistic path to a genuine overlap between the two requests.
  act(() => { input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })) })
  await flush()

  expect(container.textContent).toContain('B truncated')
  expect(container.textContent).not.toContain('A truncated')

  // The stale initial request finally resolves — it must not replace the
  // search result the user is now looking at.
  await act(async () => { pendingRecent.resolve([ITEM_A]) })

  expect(container.textContent).toContain('B truncated')
  expect(container.textContent).not.toContain('Entradas recientes')
})

it('an item with no fetchable id shows its preview read-only and never enables Save', async () => {
  const noId: MemoryItem = { content_truncated: 'orphan preview' }
  api.listMemory.mockResolvedValue([noId])
  await render()
  await clickRow(0)

  expect(api.getMemoryEntry).not.toHaveBeenCalled()
  expect(editor()?.value).toBe('orphan preview')
  expect(editor()?.readOnly).toBe(true)
  expect(saveButton()?.disabled).toBe(true)
})

it('a save in flight is discarded if the user moves to a different item before it resolves', async () => {
  api.getMemoryEntry.mockImplementation((id: string) =>
    Promise.resolve({ id, target: 'agent', content: id === 'agent:0' ? 'FULL A' : 'FULL B', entry_index: 0 }))
  const pendingSave = deferred<unknown>()
  api.updateMemoryEntry.mockReturnValueOnce(pendingSave.promise)
  await render()

  await clickRow(0)
  typeIntoTextarea(editor()!, 'edited A')
  clickButton(t => t === 'Guardar cambios')
  await flush()

  // Move on to a different item while the save is still in flight.
  await clickRow(1)
  expect(editor()?.value).toBe('FULL B')

  await act(async () => { pendingSave.resolve({}) })

  // The now-open item B must not be patched with A's edited content.
  expect(editor()?.value).toBe('FULL B')
})
