import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Tabs } from './Tabs'

let root: Root
let container: HTMLDivElement
const change = vi.fn()
const tabs = [{ key: 'a', label: 'Primera', panelId: 'panel-a' }, { key: 'b', label: 'Segunda' }, { key: 'c', label: 'Tercera' }]
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  change.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => root.render(<Tabs tabs={tabs} active="a" onChange={change} trailing={<button>Acción</button>} />))
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals() })

it('keeps one tab in the tab order and trailing actions outside the tablist', () => {
  const list = container.querySelector('[role="tablist"]')!
  expect(list.querySelectorAll('[tabindex="0"]')).toHaveLength(1)
  expect(list.textContent).not.toContain('Acción')
  expect(list.querySelector('[aria-controls="panel-a"]')).not.toBeNull()
})

it('supports arrows, wrapping, Home and End without triggering background loads', () => {
  const buttons = container.querySelectorAll<HTMLButtonElement>('[role="tab"]')
  buttons[0].focus()
  const key = (value: string) => act(() => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true })))
  key('ArrowLeft'); expect(document.activeElement).toBe(buttons[2])
  key('ArrowRight'); expect(document.activeElement).toBe(buttons[0])
  key('End'); expect(document.activeElement).toBe(buttons[2])
  key('Home'); expect(document.activeElement).toBe(buttons[0])
  expect(change).not.toHaveBeenCalled()
  act(() => buttons[1].click())
  expect(change).toHaveBeenCalledWith('b')
})
