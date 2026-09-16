import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import InstallScanModal from './InstallScanModal'
import SkillDetailsModal from './SkillDetailsModal'
import type { InstallScanResponse } from '../api/types'

const scan: InstallScanResponse = { scan_id: 'test', verdict: 'WARN', score: 60, engine: 'scan', engine_label: 'Motor de prueba', requires_owner_approval: true, risks: [{ severity: 'high', category: 'Red', message: 'Solicita acceso a Internet' }] }
describe('security review dialogs', () => {
  let root: Root
  let host: HTMLDivElement
  beforeEach(() => { vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true); host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
  afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })
  const buttons = () => [...document.querySelectorAll<HTMLButtonElement>('button')]
  const approve = () => buttons().find(button => button.textContent?.includes('Aprobar e instalar'))!
  it('opens on the safe action and retains risks after a failed request', async () => {
    const submit = vi.fn().mockRejectedValue(new Error('private technical detail'))
    await act(async () => root.render(<InstallScanModal scan={scan} name="Prueba" onApprove={submit} onCancel={vi.fn()} />))
    // Base UI defers focus until the popup frame is ready. Keep the exact
    // safety assertion, but wait for the observable focus rather than racing RAF.
    await act(async () => {
      await vi.waitFor(() => expect(document.activeElement?.textContent).toBe('Cancelar'))
    })
    await act(async () => approve().click())
    expect(document.querySelector('[role=dialog]')?.textContent).toContain('Solicita acceso a Internet')
    expect(document.querySelector('[role=alert]')?.textContent).toContain('No se pudo completar')
    expect(document.body.textContent).not.toContain('private technical detail')
  })
  it('single-flights approval and blocks dismissal while the request is pending', async () => {
    let finish!: () => void
    const submit = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    const cancel = vi.fn()
    await act(async () => root.render(<InstallScanModal scan={scan} name="Prueba" onApprove={submit} onCancel={cancel} />))
    act(() => { approve().click(); approve().click() })
    expect(submit).toHaveBeenCalledTimes(1)
    expect(buttons().filter(button => ['Cancelar', 'Cerrar'].includes(button.textContent ?? '') || button.getAttribute('aria-label') === 'Cerrar').every(button => button.disabled)).toBe(true)
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(cancel).not.toHaveBeenCalled()
    await act(async () => finish())
  })
  it('restores focus on Escape and never executes displayed skill text', async () => {
    function Harness() {
      const [open, setOpen] = useState(false)
      return <><button onClick={() => setOpen(true)}>Ver habilidad</button>{open && <SkillDetailsModal details={{ package_id: 'test', instructions: '<script>doNotExecute()</script>' }} onClose={() => setOpen(false)} />}</>
    }
    await act(async () => root.render(<Harness />))
    const trigger = host.querySelector('button')!; trigger.focus()
    await act(async () => trigger.click())
    expect(document.querySelector('pre')?.textContent).toBe('<script>doNotExecute()</script>')
    expect(document.querySelector('[role=dialog] script')).toBeNull()
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(document.querySelector('[role=dialog]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
})
