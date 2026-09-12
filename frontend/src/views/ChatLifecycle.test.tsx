import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ChatOutletContext } from '../components/Layout'
import { ChatDraft } from '../lib/chatDrafts'
import { I18nProvider } from '../lib/i18n'
import ChatView from './ChatView'

vi.mock('../api/client', () => ({
  listProviders: vi.fn().mockResolvedValue([]), listSkills: vi.fn().mockResolvedValue([]),
  getRuntimeStatus: vi.fn(), ApiError: class extends Error {},
}))
vi.mock('../hooks/useFeatures', () => ({ useFeatures: () => ({ allowed: () => true }) }))
vi.mock('../components/VncView', () => ({ VncFrame: () => null }))
vi.mock('../components/ContextPanel', () => ({ default: () => null }))
vi.mock('../components/PendingApprovalsInChat', () => ({ default: () => null }))

let host: HTMLDivElement
let root: Root
let context: ChatOutletContext
async function render() {
  await act(async () => { root.render(<I18nProvider><MemoryRouter><Routes>
    <Route element={<Outlet context={context} />}><Route index element={<ChatView />} /></Route>
    <Route path="tareas" element={<p>Task destination</p>} />
  </Routes></MemoryRouter></I18nProvider>) })
}
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  context = {
    draft: new ChatDraft('test'), convId: 'conversation', agentId: null, agentName: null,
    messages: [{ type: 'user', id: 'user', text: 'Message' }],
    status: { phase: 'streaming', statusText: 'Trabajando…' },
    cancellation: 'idle', streamError: false, reconnecting: false, liveBrowserActive: false,
    sendMessage: vi.fn(), stopStream: vi.fn(), startNew: vi.fn(), startNewWithAgent: vi.fn(),
    loadConversation: vi.fn(), reloadProvider: vi.fn(), approvalRefreshTick: 0, conversationsTick: 0,
  }
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('opens the real Tasks route from an assistant link without reloading the app', async () => {
  context.status = { phase: 'idle' }
  context.messages = [{ type: 'assistant', id: 'reply', taskId: null,
    thinkingText: '', thinkingDone: true, toolSteps: [], activityText: '',
    renderedHtml: '<p><a href="/tareas">Ver Tareas</a></p>', isStreaming: false }]
  await render()
  const link = host.querySelector<HTMLAnchorElement>('a[href="/tareas"]')!
  const click = new MouseEvent('click', { bubbles: true, cancelable: true })
  await act(async () => { link.dispatchEvent(click) })
  expect(click.defaultPrevented).toBe(true)
  expect(host.textContent).toContain('Task destination')
})

it('shows unconfirmed cancellation without hiding the next draft or allowing another stop/send', async () => {
  context.cancellation = 'requested'
  context.draft.set('text', 'Next draft')
  await render()
  expect(host.textContent).toContain('Cancelación solicitada')
  expect(host.textContent).toContain('lo ya ejecutado no se deshace')
  const button = host.querySelector<HTMLButtonElement>('[aria-label="Cancelación pendiente"]')!
  expect(button.getAttribute('aria-disabled')).toBe('true')
  await act(async () => { button.click() })
  expect(context.stopStream).not.toHaveBeenCalled()
  const input = host.querySelector('textarea')!
  expect(input.value).toBe('Next draft')
  expect(input.disabled).toBe(false)
  await act(async () => { input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })) })
  expect(context.sendMessage).not.toHaveBeenCalled()
})

it('keeps focus on the pending control and returns to the draft only after terminal completion', async () => {
  await render()
  const stop = host.querySelector<HTMLButtonElement>('[aria-label="Detener generación"]')!
  act(() => stop.focus())
  context.cancellation = 'requested'
  await render()
  expect(document.activeElement).toBe(stop)
  context.cancellation = 'idle'; context.status = { phase: 'idle' }
  await render()
  expect(document.activeElement).toBe(host.querySelector('textarea'))
})

it('does not steal focus from a user who moved away while cancellation was pending', async () => {
  await render()
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="Detener generación"]')!.focus())
  const other = host.querySelector<HTMLButtonElement>('[aria-pressed]')!
  act(() => other.focus())
  context.status = { phase: 'idle' }
  await render()
  expect(document.activeElement).toBe(other)
})

it('displays interrupted reception and real cancellation retry instead of a working spinner', async () => {
  context.streamError = true
  await render()
  expect(host.querySelector('[role=alert]')?.textContent).toContain('Se interrumpió la recepción')
  expect(host.textContent).not.toContain('Trabajando…')
  context.cancellation = 'error'
  await render()
  expect(host.textContent).toContain('No se pudo confirmar la cancelación')
  const stop = host.querySelector<HTMLButtonElement>('[aria-label="Detener generación"]')!
  await act(async () => { stop.click() })
  expect(context.stopStream).toHaveBeenCalledOnce()
})

it('never mistakes an unrelated 409 conflict for missing model configuration', async () => {
  context.status = { phase: 'error', message: 'HTTP 409', code: 'another_conflict' }
  await render()
  expect(host.textContent).toContain('No se pudo completar la solicitud')
  expect(host.textContent).not.toContain('Conecta un modelo')
  expect(host.textContent).not.toContain('No se envió')
})

it('uses the actual emergency-stop error code and blocks stopping before a task is acknowledged', async () => {
  context.status = { phase: 'error', message: 'private detail', code: 'kill_switch_engaged' }
  await render()
  expect(host.textContent).toContain('El freno de emergencia está activo')
  expect(host.textContent).not.toContain('private detail')
  context.status = { phase: 'sending' }
  await render()
  const stop = host.querySelector<HTMLButtonElement>('[aria-label="Esperando a que el agente reciba la tarea"]')!
  expect(stop.getAttribute('aria-disabled')).toBe('true')
  await act(async () => { stop.click() })
  expect(context.stopStream).not.toHaveBeenCalled()
})
