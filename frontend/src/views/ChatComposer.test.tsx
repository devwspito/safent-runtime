import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../lib/i18n'
import { listSkills, uploadWorkspaceFile } from '../api/client'
import { Composer } from './ChatView'

vi.mock('../api/client', () => ({
  listProviders: vi.fn().mockResolvedValue([]),
  listSkills: vi.fn().mockResolvedValue([]),
  uploadWorkspaceFile: vi.fn(),
  getRuntimeStatus: vi.fn(),
  ApiError: class extends Error {},
}))
vi.mock('../hooks/useFeatures', () => ({ useFeatures: () => ({ allowed: () => true }) }))
vi.mock('../components/VncView', () => ({ VncFrame: () => null }))
vi.mock('../components/ContextPanel', () => ({ default: () => null }))
vi.mock('../components/PendingApprovalsInChat', () => ({ default: () => null }))

describe('Community composer — production interactions', () => {
  let host: HTMLDivElement
  let root: Root
  const send = vi.fn()
  const stop = vi.fn()

  async function mount(initial = 'Mensaje', busy = false) {
    function Harness() {
      const [value, setValue] = useState(initial)
      return <I18nProvider><MemoryRouter><Composer value={value} onChange={setValue}
        disabled={busy} isStreaming={busy} onSend={send} onStop={stop} /></MemoryRouter></I18nProvider>
    }
    await act(async () => { root.render(<Harness />) })
  }
  function button(label: string) {
    const found = [...host.querySelectorAll('button')].find(el => el.getAttribute('aria-label') === label || el.textContent === label)
    expect(found, label).toBeDefined()
    return found!
  }
  async function click(el: Element) { await act(async () => { (el as HTMLElement).click() }) }
  async function key(el: Element, key: string, options: KeyboardEventInit = {}) {
    await act(async () => { el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...options })) })
  }
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    host = document.createElement('div')
    document.body.append(host)
    root = createRoot(host)
  })
  afterEach(() => { act(() => root.unmount()); host.remove() })

  it('does not submit Enter used to commit IME text or Shift+Enter', async () => {
    await mount()
    const input = host.querySelector('textarea')!
    await key(input, 'Enter', { isComposing: true })
    await key(input, 'Enter', { shiftKey: true })
    expect(send).not.toHaveBeenCalled()
    await key(input, 'Enter')
    expect(send).toHaveBeenCalledExactlyOnceWith('Mensaje')
  })

  it('keeps the next draft editable while working but cannot send it prematurely', async () => {
    await mount('Siguiente mensaje', true)
    expect(host.querySelector('textarea')!.disabled).toBe(false)
    await key(host.querySelector('textarea')!, 'Enter')
    expect(send).not.toHaveBeenCalled()
    await click(button('Detener generación'))
    expect(stop).toHaveBeenCalledOnce()
  })

  it('does not submit twice before the parent has rendered its busy state', async () => {
    await mount()
    const submit = host.querySelector<HTMLButtonElement>('button[aria-label="Enviar mensaje (Enter)"]')!
    await act(async () => { submit.click(); submit.click() })
    expect(send).toHaveBeenCalledExactlyOnceWith('Mensaje')
  })

  it('blocks keyboard sends while upload is pending and after an upload fails', async () => {
    let rejectUpload!: (reason: Error) => void
    vi.mocked(uploadWorkspaceFile).mockReturnValue(new Promise((_resolve, reject) => { rejectUpload = reject }))
    await mount()
    const file = host.querySelector<HTMLInputElement>('input[type=file]')!
    Object.defineProperty(file, 'files', { value: [new File(['x'], 'brief.txt', { type: 'text/plain' })] })
    await act(async () => { file.dispatchEvent(new Event('change', { bubbles: true })) })
    await key(host.querySelector('textarea')!, 'Enter')
    expect(send).not.toHaveBeenCalled()
    await act(async () => { rejectUpload(new Error('upload unavailable')) })
    expect(host.querySelector('[role=alert]')?.textContent).toContain('no se han subido')
    await key(host.querySelector('textarea')!, 'Enter')
    expect(send).not.toHaveBeenCalled()
  })

  it('moves focus through the context menu and restores its trigger on Escape', async () => {
    await mount()
    const trigger = host.querySelector<HTMLButtonElement>('[aria-haspopup=menu]')!
    await click(trigger)
    const items = host.querySelectorAll('[role=menuitem]')
    expect(document.activeElement).toBe(items[0])
    await key(items[0]!, 'ArrowDown')
    expect(document.activeElement).toBe(items[1])
    await key(items[1]!, 'End')
    expect(document.activeElement).toBe(items[2])
    await key(items[2]!, 'Escape')
    expect(host.querySelector('[role=menu]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('distinguishes skills-load failure from an empty list and lets the user retry', async () => {
    vi.mocked(listSkills).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([])
    await mount()
    await click(host.querySelector('[aria-haspopup=menu]')!)
    await click(button('Habilidades'))
    expect(host.textContent).toContain('No se pudieron cargar las habilidades')
    await click(button('Reintentar'))
    expect(listSkills).toHaveBeenCalledTimes(2)
    expect(host.textContent).toContain('Ninguna')
    expect(host.textContent).not.toContain('No se pudieron cargar las habilidades')
  })
})
