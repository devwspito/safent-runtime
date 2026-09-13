import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ChatOutletContext } from '../components/Layout'
import { ChatDraft } from '../lib/chatDrafts'
import { I18nProvider } from '../lib/i18n'
import ChatView from './ChatView'
import { getNativeActive, listProviders } from '../api/client'

const features = vi.hoisted(() => ({ providers: true }))

vi.mock('../api/client', () => ({
  listProviders: vi.fn().mockResolvedValue([]), listSkills: vi.fn().mockResolvedValue([]),
  getNativeActive: vi.fn().mockResolvedValue(null),
  getRuntimeStatus: vi.fn(), ApiError: class extends Error {},
}))
vi.mock('../hooks/useFeatures', () => ({ useFeatures: () => ({ allowed: (name: string) => name !== 'proveedores' || features.providers }) }))
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
  features.providers = true
  vi.mocked(listProviders).mockReset().mockResolvedValue([])
  vi.mocked(getNativeActive).mockReset().mockResolvedValue(null)
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

it('a terminal failure replaces stale reception warnings and restores Send without resending the draft', async () => {
  context.draft.set('text', 'Next draft')
  context.status = { phase: 'error', message: 'Task failed', code: 'task_failed' }
  context.streamError = true; context.reconnecting = true
  await render()
  expect(host.textContent).not.toContain('Seguimos consultando')
  expect(host.querySelector('[aria-label="Detener generación"]')).toBeNull()
  expect(host.querySelector<HTMLButtonElement>('[aria-label="Enviar mensaje (Enter)"]')?.disabled).toBe(false)
  expect(host.querySelector('textarea')?.value).toBe('Next draft')
  expect(context.sendMessage).not.toHaveBeenCalled()
})

it('confirmed cancellation shows a quiet terminal status with no stop spinner or duplicate alert', async () => {
  context.status = { phase: 'idle', outcome: 'cancelled' }
  context.streamError = true; context.cancellation = 'requested'
  await render()
  expect(host.textContent).toContain('Tarea detenida. No se ha reenviado')
  expect(host.textContent).not.toContain('Cancelación solicitada')
  expect(host.textContent).not.toContain('Seguimos consultando')
  expect(host.querySelector('[aria-label="Detener generación"]')).toBeNull()
  expect(host.querySelector('[role="alert"]')).toBeNull()
  expect(host.querySelector('[role="status"] svg')).toBeNull()
})

it('never labels the first stored but inactive provider as the active chat model', async () => {
  vi.mocked(listProviders).mockResolvedValue([{ provider_id: 'inactive', alias: 'Inactive saved model', default_model: 'inactive-model', is_active: false }])
  context.status = { phase: 'idle' }
  await render()
  expect(host.textContent).not.toContain('inactive-model')
  expect(host.textContent).toContain('Sin modelo')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('El agente no tiene modelo de IA conectado')
  expect(getNativeActive).toHaveBeenCalledOnce()
  expect(listProviders).not.toHaveBeenCalled()
})

it('uses only the actually active provider for chat model metadata', async () => {
  vi.mocked(listProviders).mockResolvedValue([
    { provider_id: 'inactive', alias: 'Inactive', default_model: 'wrong-first-model', is_active: false },
    { provider_id: 'native-active', alias: 'Native active', default_model: 'correct-active-model', is_active: true },
  ])
  vi.mocked(getNativeActive).mockResolvedValue({ provider_id: 'native-active', default_model: 'correct-active-model', is_active: true })
  context.status = { phase: 'idle' }
  await render()
  expect(host.textContent).toContain('correct-active-model')
  expect(host.textContent).not.toContain('wrong-first-model')
  expect(host.textContent).not.toContain('El agente no tiene modelo de IA conectado')
  expect(getNativeActive).toHaveBeenCalledOnce()
  expect(listProviders).not.toHaveBeenCalled()
})

it('refreshes the model chip and missing-model hint together after activation without sending chat', async () => {
  vi.useFakeTimers()
  try {
    vi.mocked(getNativeActive).mockResolvedValueOnce(null)
      .mockResolvedValue({ provider_id: 'saved', default_model: 'newly-active-model', is_active: true })
    context.status = { phase: 'idle' }
    await render()
    expect(host.textContent).not.toContain('newly-active-model')
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(host.textContent).toContain('newly-active-model')
    expect(host.textContent).not.toContain('El agente no tiene modelo de IA conectado')
    expect(getNativeActive).toHaveBeenCalledTimes(2)
    expect(context.sendMessage).not.toHaveBeenCalled()
  } finally { vi.useRealTimers() }
})

it('does not turn a provider lookup error into a missing-model claim', async () => {
  vi.mocked(getNativeActive).mockRejectedValue(new Error('private provider failure'))
  vi.mocked(listProviders).mockResolvedValue([{ provider_id: 'stale', default_model: 'stale-active-model', is_active: true }])
  context.status = { phase: 'idle' }
  await render()
  expect(host.textContent).toContain('Estado del modelo no disponible')
  expect(host.textContent).not.toContain('El agente no tiene modelo de IA conectado')
  expect(host.textContent).not.toContain('private provider failure')
  expect(host.textContent).not.toContain('stale-active-model')
})

it('shows the effective Codex model even when SQL still marks Qwen active', async () => {
  vi.mocked(listProviders).mockResolvedValue([{ provider_id: 'custom-qwen', default_model: 'qwen3.8-27b', is_active: true }])
  vi.mocked(getNativeActive).mockResolvedValue({ provider_id: 'openai-codex', default_model: 'gpt-codex-effective', is_active: true })
  context.status = { phase: 'idle' }
  await render()
  expect(host.textContent).toContain('gpt-codex-effective')
  expect(host.textContent).not.toContain('qwen3.8-27b')
  expect(host.textContent).not.toContain('El agente no tiene modelo de IA conectado')
  expect(listProviders).not.toHaveBeenCalled()
})

it('does not revive a stale SQL active flag when the engine has no selection', async () => {
  vi.mocked(listProviders).mockResolvedValue([{ provider_id: 'stale', default_model: 'stale-active-model', is_active: true }])
  context.status = { phase: 'idle' }
  await render()
  expect(host.textContent).toContain('Sin modelo')
  expect(host.textContent).not.toContain('stale-active-model')
})

it('keeps the Enterprise managed model inert without claiming missing local configuration', async () => {
  features.providers = false
  vi.mocked(listProviders).mockResolvedValue([{ provider_id: 'inactive', default_model: 'wrong-local-model', is_active: false }])
  context.status = { phase: 'idle' }
  await render()
  expect(host.textContent).not.toContain('wrong-local-model')
  expect(host.textContent).not.toContain('El agente no tiene modelo de IA conectado')
  expect(host.textContent).not.toContain('Sin modelo')
})
