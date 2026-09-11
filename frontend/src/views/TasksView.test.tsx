import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getTaskDashboard, getTaskInbox } from '../api/client'
import { I18nProvider } from '../lib/i18n'
import TasksView from './TasksView'

vi.mock('../api/client', () => ({ getTaskDashboard: vi.fn(), getTaskInbox: vi.fn() }))
vi.mock('../hooks/usePendingApprovals', () => ({ usePendingApprovals: () => ({ approvals: [], isLoading: false, error: false, refresh: vi.fn() }) }))
vi.mock('../components/ApprovalCard', () => ({ default: () => <button>Resolver acción</button> }))
vi.mock('../components/InboundDelegationCard', () => ({ default: () => <button>Aceptar encargo</button> }))
vi.mock('./CalendarView', () => ({ default: () => <div>Calendario existente</div> }))
const task = { task_id: 'task-1', label: 'Revisar campaña', status: 'completed' as const, source: 'enterprise' as const, requested_by: 'Luis', conversation_id: 'chat-1', result: 'Informe listo' }

describe('Community task dashboard', () => {
  let host: HTMLDivElement
  let root: Root
  const open = vi.fn().mockResolvedValue(undefined)
  async function render(path = '/tareas') {
    await act(async () => root.render(<I18nProvider><MemoryRouter initialEntries={[path]}><Routes>
      <Route element={<Outlet context={{ loadConversation: open }} />}><Route path="/tareas" element={<TasksView />} /><Route path="/chat" element={<div>Conversación abierta</div>} /></Route>
    </Routes></MemoryRouter></I18nProvider>))
  }
  async function click(label: string) {
    await act(async () => {
      const button = [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.includes(label) || item.getAttribute('aria-label') === label)
      expect(button).toBeTruthy(); button!.click()
    })
  }
  beforeEach(() => {
    vi.clearAllMocks(); localStorage.clear()
    vi.mocked(getTaskInbox).mockResolvedValue([])
    vi.mocked(getTaskDashboard).mockResolvedValue({ available: true, tasks: [task], has_more: false })
    host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  })
  afterEach(() => { act(() => root.unmount()); host.remove() })

  it('distinguishes an unavailable service from an empty dashboard and supports retry', async () => {
    vi.mocked(getTaskDashboard).mockRejectedValueOnce(new Error('404')).mockResolvedValueOnce({ available: true, tasks: [], has_more: false })
    await render()
    expect(host.textContent).toContain('Esto no significa que no tengas encargos')
    expect(host.textContent).not.toContain('Todavía no hay tareas registradas')
    await click('Actualizar tareas')
    expect(host.textContent).toContain('Todavía no hay tareas registradas')
  })
  it('shows provenance and result, never assumes missing approval linkage means none', async () => {
    await render(); await click('Revisar campaña')
    expect(host.textContent).toContain('Luis')
    expect(host.textContent).toContain('Informe listo')
    expect(host.textContent).toContain('El servidor aún no informa qué aprobaciones')
    await click('Abrir conversación')
    expect(open).toHaveBeenCalledWith('chat-1')
    expect(host.textContent).toContain('Conversación abierta')
  })
  it('preserves last known data but blocks actions after refresh failure', async () => {
    await render(); await click('Revisar campaña')
    vi.mocked(getTaskDashboard).mockRejectedValue(new Error('offline'))
    await click('Actualizar tareas')
    expect(host.textContent).toContain('Informe listo')
    expect(host.textContent).toContain('Las acciones están bloqueadas')
    const button = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('Abrir conversación'))!
    expect(button.disabled).toBe(true)
  })
  it.each([{ ...task, status: 'toString' }, { ...task, result: {} }, { ...task, task_id: 1 }, null])('rejects malformed task records', async bad => {
    vi.mocked(getTaskDashboard).mockResolvedValue({ available: true, tasks: [bad], has_more: false } as never)
    await render()
    expect(host.textContent).toContain('No se pudo consultar el cuadro')
    expect(host.textContent).not.toContain('Todavía no hay tareas registradas')
  })
  it('filters completed work without treating the global list as empty', async () => {
    await render(); await click('En curso')
    expect(host.textContent).toContain('No hay tareas que coincidan con este filtro')
    expect(host.textContent).toContain('1 tareas recientes')
  })
  it('keeps scheduled tasks in the same view without invoking dashboard APIs', async () => {
    await render('/tareas?tab=programadas')
    expect(host.textContent).toContain('Calendario existente')
    expect(getTaskDashboard).not.toHaveBeenCalled()
  })
})
