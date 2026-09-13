import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CalendarView from './CalendarView'
import { I18nProvider } from '../lib/i18n'
import { createTask, listAgents, listConfiguredTasks, listRecentTasks } from '../api/client'

vi.mock('../api/client', () => ({
  createTask: vi.fn(), listAgents: vi.fn(), listConfiguredTasks: vi.fn(), listRecentTasks: vi.fn(),
  deleteTask: vi.fn(), toggleTask: vi.fn(), ApiError: class extends Error {},
}))
vi.mock('sileo', () => ({ sileo: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
let root: Root
let host: HTMLDivElement
beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(listAgents).mockResolvedValue([])
  vi.mocked(listConfiguredTasks).mockResolvedValue({ available: true, tasks: [] })
  vi.mocked(listRecentTasks).mockResolvedValue({ available: true, tasks: [] })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  await act(async () => root.render(<I18nProvider><CalendarView /></I18nProvider>))
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })
function button(text: string) {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].find(node => node.textContent === text || node.getAttribute('aria-label') === text)!
}
async function open() {
  const trigger = button('Nueva tarea')
  trigger.focus()
  await act(async () => trigger.click())
  return trigger
}
async function escape() {
  await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
}

it('Escape closes the actual new-task dialog and restores its trigger', async () => {
  const trigger = await open()
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  expect(document.activeElement).toBe(document.getElementById('tm-name'))
  await escape()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})

it.each(['new-task button', 'calendar day'])(
  'Escape returns to the clicked %s when Safari leaves focus on another control',
  async (kind) => {
    const previous = button('Hoy')
    previous.focus()
    const trigger = kind === 'new-task button'
      ? button('Nueva tarea')
      : document.querySelector<HTMLElement>('[data-date][role="button"]')!
    expect(document.activeElement).toBe(previous)
    expect(document.activeElement).not.toBe(trigger)
    // Native .click(), unlike user-event, does not first focus the trigger.
    // Clicking a nested day label also verifies currentTarget, not target.
    const clickTarget = kind === 'calendar day' ? trigger.querySelector('span')! : trigger
    await act(async () => clickTarget.click())
    await act(async () => new Promise<void>(resolve => requestAnimationFrame(() => resolve())))
    expect(document.activeElement).toBe(document.getElementById('tm-name'))
    await escape()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(createTask).not.toHaveBeenCalled()
  },
)

it('uses modal focus guards to cycle back inside instead of reaching background controls', async () => {
  await open()
  const dialog = document.querySelector('[role="dialog"]')!
  const guards = [...document.querySelectorAll<HTMLElement>('[data-base-ui-focus-guard][data-type="inside"]')]
  expect(guards.length).toBeGreaterThanOrEqual(2)
  for (const guard of guards) {
    await act(async () => {
      guard.focus()
      // Base UI enqueues its boundary-focus handoff on the next animation frame.
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    })
    expect(dialog.contains(document.activeElement)).toBe(true)
  }
})

it('Escape, close and Cancel cannot dismiss an in-flight creation; failure remains recoverable', async () => {
  let reject!: (error: Error) => void
  vi.mocked(createTask).mockImplementation(() => new Promise((_, fail) => { reject = fail }))
  const trigger = await open()
  ;(document.getElementById('tm-name') as HTMLInputElement).value = 'Tarea de prueba'
  ;(document.getElementById('tm-prompt') as HTMLTextAreaElement).value = 'Revisar el informe'
  await act(async () => button('Todos los días').click())
  await act(async () => button('Crear tarea').click())
  expect(createTask).toHaveBeenCalledTimes(1)
  expect(button('Cancelar').disabled).toBe(true)
  expect(button('Cerrar diálogo').disabled).toBe(true)
  await escape()
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  await act(async () => reject(new Error('Fallo de prueba')))
  expect(button('Cancelar').disabled).toBe(false)
  await escape()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})
