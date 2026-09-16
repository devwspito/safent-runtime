import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useConfirmDialog } from './ConfirmDialog'
let root: Root
let container: HTMLDivElement
let confirm: ReturnType<typeof useConfirmDialog>[0]
function Harness() { const [open, node] = useConfirmDialog(); confirm = open; return <><button>Origen</button>{node}</> }
beforeEach(() => { vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true); container = document.createElement('div'); document.body.append(container); root = createRoot(container); act(() => root.render(<Harness />)) })
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals() })
it('declines a replaced request rather than leaving its promise unresolved', async () => {
  const first = vi.fn()
  act(() => { void confirm({ title: 'Primera' }).then(first) })
  await act(async () => { void confirm({ title: 'Segunda' }) })
  expect(first).toHaveBeenCalledWith(false)
  expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain('Segunda')
})
it('unmount resolves a pending decision as declined, never approved', async () => {
  const result = vi.fn()
  act(() => { void confirm({ title: 'Pendiente' }).then(result) })
  await act(async () => root.render(<div />))
  expect(result).toHaveBeenCalledWith(false)
})
it('Escape cancels and restores focus to the original trigger', async () => {
  const trigger = container.querySelector('button')!
  trigger.focus()
  const result = vi.fn()
  await act(async () => { void confirm({ title: 'Permitir', description: 'Una sola vez' }).then(result) })
  expect(document.activeElement?.textContent).toBe('Cancelar')
  await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(result).toHaveBeenCalledWith(false)
  expect(document.activeElement).toBe(trigger)
})
