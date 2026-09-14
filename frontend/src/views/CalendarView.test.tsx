import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CalendarView from './CalendarView'
import { I18nProvider } from '../lib/i18n'
import { createTask, updateTask, listAgents, listConfiguredTasks, listRecentTasks } from '../api/client'
import type { ConfiguredTask } from '../api/types'

vi.mock('../api/client', () => ({
  createTask: vi.fn(), updateTask: vi.fn(), listAgents: vi.fn(), listConfiguredTasks: vi.fn(), listRecentTasks: vi.fn(),
  deleteTask: vi.fn(), toggleTask: vi.fn(), ApiError: class extends Error {},
}))
vi.mock('sileo', () => ({ sileo: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
// jsdom never fires the WAAPI completion events framer-motion's AnimatePresence
// waits on, so a real `mode="wait"` view-mode swap (board → list) never
// settles here. The view-mode logic under test is which panel is chosen and
// what it renders — not the transition — so render every motion primitive as
// a plain, immediate passthrough.
vi.mock('../components/ui/motion', () => {
  function Passthrough({ children, initial, animate, exit, transition, layout, layoutId, variants, whileHover, whileTap, ...rest }: Record<string, unknown> & { children?: React.ReactNode }) {
    return <div {...rest}>{children}</div>
  }
  return {
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    AnimatedListItem: ({ children, ...rest }: Record<string, unknown> & { children?: React.ReactNode }) => <li {...rest}>{children}</li>,
    Stagger: ({ children, ...rest }: Record<string, unknown> & { children?: React.ReactNode }) => <div {...rest}>{children}</div>,
    StaggerItem: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    HoverRow: ({ children, ...rest }: Record<string, unknown> & { children?: React.ReactNode }) => <div {...rest}>{children}</div>,
    motion: new Proxy({}, { get: () => Passthrough }),
    SPRING: {},
  }
})
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
/** Re-renders with a given set of already-configured tasks (the default beforeEach starts empty). */
async function renderWithTasks(tasks: ConfiguredTask[]) {
  vi.mocked(listConfiguredTasks).mockResolvedValue({ available: true, tasks })
  await act(async () => root.unmount())
  root = createRoot(host)
  await act(async () => root.render(<I18nProvider><CalendarView /></I18nProvider>))
}
function selectOption(id: string, value: string) {
  return act(async () => {
    const select = document.getElementById(id) as HTMLSelectElement
    select.value = value
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
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

// ── Frequency presets ────────────────────────────────────────────────────────

it('creates an hourly task with a fixed cron and no time picker', async () => {
  await open()
  ;(document.getElementById('tm-name') as HTMLInputElement).value = 'Vigilancia'
  ;(document.getElementById('tm-prompt') as HTMLTextAreaElement).value = 'Revisar logs de error'
  await selectOption('tm-mode', 'hourly')
  expect(document.getElementById('tm-time')).toBeNull()
  await act(async () => button('Crear tarea').click())
  expect(createTask).toHaveBeenCalledWith(expect.objectContaining({ cron: '0 * * * *', one_shot: false }))
})

it('creates a daily task at the chosen time', async () => {
  await open()
  ;(document.getElementById('tm-name') as HTMLInputElement).value = 'Resumen diario'
  ;(document.getElementById('tm-prompt') as HTMLTextAreaElement).value = 'Enviar el resumen del día'
  await selectOption('tm-mode', 'daily')
  await act(async () => {
    const time = document.getElementById('tm-time') as HTMLInputElement
    // Controlled input: React tracks the DOM value on the instance, so a
    // plain assignment is invisible to it — set through the native
    // (unpatched) prototype setter before dispatching, same as elsewhere.
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(time, '18:30')
    time.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => button('Crear tarea').click())
  expect(createTask).toHaveBeenCalledWith(expect.objectContaining({ cron: '30 18 * * *', one_shot: false }))
})

it('an hourly task reads "Cada hora" in the list and on every day of the calendar', async () => {
  await renderWithTasks([{ trigger_id: 'trig-hourly', label: 'Vigilar servidor', cron: '0 * * * *', enabled: true }])
  // Board (calendar) is the default view: the fixed 42-cell grid shows the
  // "Cada hora" badge on every one of its days, never a specific clock time.
  const gridText = document.querySelector('.cal')?.textContent ?? ''
  expect(gridText.split('Cada hora').length - 1).toBeGreaterThan(20)
  expect(gridText).not.toMatch(/\d{2}:\d{2}/)
  // List view: the same recurrence label appears once, under the task name.
  await act(async () => button('Lista').click())
  expect(host.textContent).toContain('Vigilar servidor')
  expect(host.textContent).toContain('Cada hora')
})

// ── Template chooser ─────────────────────────────────────────────────────────

it('the "Revisar campañas" template fills the name, instruction and a daily 09:00 cron', async () => {
  await open()
  await selectOption('tm-template', 'ads-review')
  expect((document.getElementById('tm-name') as HTMLInputElement).value).toBe('Revisión de campañas')
  expect((document.getElementById('tm-prompt') as HTMLTextAreaElement).value).toContain('Revisa todas las campañas activas de anuncios')
  expect((document.getElementById('tm-mode') as HTMLSelectElement).value).toBe('daily')
  await act(async () => button('Crear tarea').click())
  expect(createTask).toHaveBeenCalledWith(expect.objectContaining({
    label: 'Revisión de campañas', cron: '0 9 * * *', one_shot: false,
  }))
})

// ── Editing pre-selects the right preset ─────────────────────────────────────

it('editing an hourly task pre-selects "Cada hora" and hides the time picker', async () => {
  await renderWithTasks([{ trigger_id: 'trig-1', label: 'Vigilar servidor', cron: '0 * * * *', instruction: 'Revisar', enabled: true }])
  await act(async () => button('Lista').click())
  await act(async () => button('Editar').click())
  expect((document.getElementById('tm-mode') as HTMLSelectElement).value).toBe('hourly')
  expect(document.getElementById('tm-time')).toBeNull()
})

it('editing a task on specific days pre-selects them along with its time, and saves via updateTask', async () => {
  await renderWithTasks([{
    trigger_id: 'trig-2', label: 'Informe semanal', cron: '30 14 * * 1,3', instruction: 'Enviar informe', enabled: true,
  }])
  await act(async () => button('Lista').click())
  await act(async () => button('Editar').click())
  expect((document.getElementById('tm-mode') as HTMLSelectElement).value).toBe('weekly')
  expect((document.getElementById('tm-time') as HTMLInputElement).value).toBe('14:30')
  const pressedDays = [...document.querySelectorAll('button[aria-pressed="true"]')].map(b => b.textContent)
  expect(pressedDays).toEqual(expect.arrayContaining(['Lun', 'Mié']))
  expect(pressedDays).not.toEqual(expect.arrayContaining(['Mar']))
  await act(async () => button('Guardar cambios').click())
  expect(updateTask).toHaveBeenCalledWith('trig-2', expect.objectContaining({ cron: '30 14 * * 1,3', one_shot: false }))
})

it('editing a one-shot task pre-selects "Una vez" with its date', async () => {
  await renderWithTasks([{
    trigger_id: 'trig-3', label: 'Migración puntual', cron: '0 10 24 12 *', one_shot: true,
    next_run_at: '2026-12-24T10:00:00', instruction: 'Migrar datos', enabled: true,
  }])
  await act(async () => button('Lista').click())
  await act(async () => button('Editar').click())
  expect((document.getElementById('tm-mode') as HTMLSelectElement).value).toBe('once')
  expect((document.getElementById('tm-date') as HTMLInputElement).value).toBe('2026-12-24')
  await act(async () => button('Guardar cambios').click())
  expect(updateTask).toHaveBeenCalledWith('trig-3', expect.objectContaining({ one_shot: true, label: 'Migración puntual' }))
})
