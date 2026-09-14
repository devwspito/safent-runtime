import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../lib/i18n'
import { listConversations, archiveConversation, unarchiveConversation, deleteConversation } from '../api/client'
import { RecentsSection } from './Layout'

const { sileoError } = vi.hoisted(() => ({ sileoError: vi.fn() }))
vi.mock('../api/client', () => ({
  listConversations: vi.fn(),
  archiveConversation: vi.fn(),
  unarchiveConversation: vi.fn(),
  deleteConversation: vi.fn(),
}))
vi.mock('sileo', () => ({ sileo: { error: sileoError, success: vi.fn(), warning: vi.fn() } }))
vi.mock('./NotificationsPanel', () => ({ default: () => null }))
vi.mock('./KillSwitchBanner', () => ({ default: () => null }))
vi.mock('./SystemUpdateFooter', () => ({ SystemUpdateFooter: () => null }))
vi.mock('../views/sectionHubIds', () => ({ CAPACIDADES_VIEW_IDS: [], SISTEMA_VIEW_IDS: [] }))

describe('Community recent conversations', () => {
  let host: HTMLDivElement
  let root: Root
  const open = vi.fn().mockResolvedValue(undefined)
  const startNew = vi.fn()
  async function render(tick = 0) {
    await act(async () => { root.render(<I18nProvider><MemoryRouter><RecentsSection
      activeConvId="first" conversationsTick={tick} loadConversation={open} startNew={startNew} /></MemoryRouter></I18nProvider>) })
  }
  async function click(el: Element) { await act(async () => { (el as HTMLElement).click() }) }
  async function key(el: Element, keyName: string) {
    await act(async () => { el.dispatchEvent(new KeyboardEvent('keydown', { key: keyName, bubbles: true, cancelable: true })) })
  }
  function button(label: string) {
    const found = [...host.querySelectorAll('button')].find(el => el.getAttribute('aria-label') === label || el.textContent === label)
    expect(found, label).toBeDefined()
    return found!
  }
  function menuItem(label: string) {
    const found = [...host.querySelectorAll('[role=menuitem]')].find(el => el.textContent?.includes(label))
    expect(found, label).toBeDefined()
    return found as HTMLButtonElement
  }
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  })
  afterEach(() => { act(() => root.unmount()); host.remove() })

  it('shows a recoverable load failure, never a false empty state', async () => {
    vi.mocked(listConversations).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([])
    await render()
    expect(host.textContent).toContain('No se pudieron cargar las conversaciones')
    expect(host.textContent).not.toContain('Sin conversaciones recientes')
    await act(async () => { host.querySelector<HTMLButtonElement>('button')!.click() })
    expect(listConversations).toHaveBeenCalledTimes(2)
    expect(host.textContent).toContain('Sin conversaciones recientes')
  })

  it('keeps known conversations after a refresh failure, with semantic active navigation', async () => {
    vi.mocked(listConversations).mockResolvedValueOnce([{ id: 'first', title: 'Plan de marca' }]).mockRejectedValueOnce(new Error('offline'))
    await render()
    expect(host.querySelector('[aria-current=page]')?.textContent).toContain('Plan de marca')
    expect(host.querySelector('[role=listbox]')).toBeNull()
    await render(1)
    expect(host.textContent).toContain('Plan de marca')
    expect(host.textContent).toContain('No se pudieron cargar las conversaciones')
  })

  it('ignores an older response after the active list was refreshed', async () => {
    let resolveOld!: (rows: Awaited<ReturnType<typeof listConversations>>) => void
    vi.mocked(listConversations).mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValueOnce([{ id: 'first', title: 'Actual' }])
    await render()
    await render(1)
    await act(async () => { resolveOld([{ id: 'old', title: 'Obsoleto' }]) })
    expect(host.textContent).toContain('Actual')
    expect(host.textContent).not.toContain('Obsoleto')
  })

  it('opens the row menu by mouse, and keeps it keyboard-operable', async () => {
    vi.mocked(listConversations).mockResolvedValue([{ id: 'first', title: 'Plan de marca' }])
    await render()
    const trigger = host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!
    expect(trigger.tabIndex).not.toBe(-1)

    // Mouse: click opens it, focus lands on the first item.
    await click(trigger)
    const items = host.querySelectorAll('[role=menuitem]')
    expect(items).toHaveLength(2)
    expect(document.activeElement).toBe(items[0])
    await key(items[0]!, 'Escape')
    expect(host.querySelector('[role=menu]')).toBeNull()
    expect(document.activeElement).toBe(trigger)

    // Keyboard: a focused native <button> activates on Enter/Space, same as a click.
    trigger.focus()
    await click(trigger)
    expect(host.querySelectorAll('[role=menuitem]')).toHaveLength(2)
    expect(document.activeElement).toBe(host.querySelectorAll('[role=menuitem]')[0])
  })

  it('archives immediately and restores the row when the undo notice is used', async () => {
    vi.mocked(listConversations).mockResolvedValue([{ id: 'first', title: 'Plan de marca' }])
    vi.mocked(archiveConversation).mockResolvedValue(undefined)
    vi.mocked(unarchiveConversation).mockResolvedValue(undefined)
    await render()
    await click(host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!)
    await click(menuItem('Archivar'))
    expect(archiveConversation).toHaveBeenCalledWith('first')
    expect(host.querySelector('[title="Plan de marca"]')).toBeNull()
    expect(host.textContent).toContain('Conversación archivada')
    expect(startNew).toHaveBeenCalledOnce()

    await click(button('Deshacer'))
    expect(unarchiveConversation).toHaveBeenCalledWith('first')
    expect(host.querySelector('[title="Plan de marca"]')).not.toBeNull()
    expect(host.textContent).not.toContain('Conversación archivada')
  })

  it('deletes only after the inline confirmation, and Escape backs out of it', async () => {
    vi.mocked(listConversations).mockResolvedValue([{ id: 'first', title: 'Plan de marca' }])
    vi.mocked(deleteConversation).mockResolvedValue(undefined)
    await render()
    const trigger = host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!
    await click(trigger)
    await click(menuItem('Eliminar'))
    expect(deleteConversation).not.toHaveBeenCalled()
    expect(host.textContent).toContain('¿Eliminar esta conversación?')
    expect(document.activeElement?.textContent).toBe('Cancelar')

    await key(document.activeElement!, 'Escape')
    expect(host.textContent).not.toContain('¿Eliminar esta conversación?')
    expect(host.querySelector('[title="Plan de marca"]')).not.toBeNull()
    // The confirm row unmounts the original trigger and remounts a fresh one —
    // re-query it rather than reuse the (now detached) reference.
    const triggerAfterCancel = host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!
    expect(document.activeElement).toBe(triggerAfterCancel)

    await click(triggerAfterCancel)
    await click(menuItem('Eliminar'))
    await click(button('Eliminar'))
    expect(deleteConversation).toHaveBeenCalledWith('first')
    expect(host.querySelector('[title="Plan de marca"]')).toBeNull()
    expect(startNew).toHaveBeenCalledOnce()
  })

  it('keeps the row and offers a retry when deleting fails', async () => {
    vi.mocked(listConversations).mockResolvedValue([{ id: 'first', title: 'Plan de marca' }])
    vi.mocked(deleteConversation).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined)
    await render()
    await click(host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!)
    await click(menuItem('Eliminar'))
    await click(button('Eliminar'))
    expect(host.textContent).toContain('No se pudo eliminar.')
    expect(host.querySelectorAll('.sidebar-recents ul > li')).toHaveLength(1)

    await click(button('Reintentar'))
    expect(deleteConversation).toHaveBeenCalledTimes(2)
    expect(host.querySelector('[title="Plan de marca"]')).toBeNull()
  })

  it('lists archived conversations behind the toggle, with a restore action', async () => {
    vi.mocked(listConversations).mockResolvedValue([
      { id: 'first', title: 'Plan de marca' },
      { id: 'old', title: 'Vieja idea', archived: true },
    ])
    vi.mocked(unarchiveConversation).mockResolvedValue(undefined)
    await render()
    expect(host.textContent).not.toContain('Vieja idea')

    await click(button('Ver archivadas (1)'))
    expect(host.textContent).toContain('Vieja idea')
    expect(host.textContent).not.toContain('Plan de marca')
    expect(host.querySelector('[title="Vieja idea"]')?.textContent).toContain('Archivada')

    await click(host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!)
    expect(menuItem('Restaurar')).toBeDefined()
    await click(menuItem('Restaurar'))
    expect(unarchiveConversation).toHaveBeenCalledWith('old')
    expect(host.querySelector('[title="Vieja idea"]')).toBeNull()
  })
})
