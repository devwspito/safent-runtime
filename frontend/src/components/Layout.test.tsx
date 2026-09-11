import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../lib/i18n'
import { listConversations } from '../api/client'
import { RecentsSection } from './Layout'

vi.mock('../api/client', () => ({ listConversations: vi.fn() }))
vi.mock('./NotificationsPanel', () => ({ default: () => null }))
vi.mock('./KillSwitchBanner', () => ({ default: () => null }))
vi.mock('./SystemUpdateFooter', () => ({ SystemUpdateFooter: () => null }))
vi.mock('../views/sectionHubIds', () => ({ CAPACIDADES_VIEW_IDS: [], SISTEMA_VIEW_IDS: [] }))

describe('Community recent conversations', () => {
  let host: HTMLDivElement
  let root: Root
  const open = vi.fn().mockResolvedValue(undefined)
  async function render(tick = 0) {
    await act(async () => { root.render(<I18nProvider><MemoryRouter><RecentsSection
      activeConvId="first" conversationsTick={tick} loadConversation={open} /></MemoryRouter></I18nProvider>) })
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
})
