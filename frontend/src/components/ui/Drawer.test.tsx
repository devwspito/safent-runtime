import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Drawer } from './Drawer'
let root: Root
let container: HTMLDivElement
function Harness() {
  const [open, setOpen] = useState(false)
  return <><button onClick={() => setOpen(true)}>Abrir</button><Drawer open={open} title="Configuración" onClose={() => setOpen(false)} footer={<button>Guardar</button>}><button disabled>No disponible</button><input aria-label="Nombre" /></Drawer></>
}
beforeEach(() => { vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true); container = document.createElement('div'); document.body.append(container); root = createRoot(container); act(() => root.render(<Harness />)) })
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals() })
it('provides named dialog, safe initial focus, Escape and return to its trigger', async () => {
  const trigger = container.querySelector('button')!
  trigger.focus()
  await act(async () => { trigger.click() })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  const dialog = document.querySelector('[role="dialog"]')!
  expect(dialog).not.toBeNull()
  expect(document.getElementById(dialog.getAttribute('aria-labelledby')!)?.textContent).toBe('Configuración')
  expect(document.activeElement?.getAttribute('aria-label')).toBe('Cerrar panel')
  await act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key:'Escape', bubbles:true })) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})
it('does not overwrite a pre-existing body scroll style while closed', () => {
  document.body.style.overflow = 'clip'
  act(() => root.render(<Harness />))
  expect(document.body.style.overflow).toBe('clip')
  document.body.style.overflow = ''
})
