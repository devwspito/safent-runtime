import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PendingApproval } from '../api/types'

const { resolve, success } = vi.hoisted(() => ({ resolve: vi.fn(), success: vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), resolveApproval: resolve }))
vi.mock('sileo', () => ({ sileo: { success } }))
import ApprovalCard from './ApprovalCard'
import { ApiError } from '../api/client'

let root: Root
let container: HTMLDivElement
const approval: PendingApproval = { proposal_id: 'proposal-1', summary: 'Actualizar un archivo', target: 'write_file', required_level: 'simple', conversation_id: 'thread-1' }
const onResolved = vi.fn()

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  resolve.mockReset().mockResolvedValue({ ok: true, live: true })
  success.mockReset()
  onResolved.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals() })

function render(value = approval) {
  act(() => root.render(<MemoryRouter><ApprovalCard approval={value} onResolved={onResolved} /></MemoryRouter>))
}
function button(text: string) {
  const match = Array.from(document.querySelectorAll('button')).find(el => el.textContent?.includes(text))
  if (!match) throw new Error(`Missing button: ${text}`)
  return match
}

it('shows the target, scope, complete parameters and justification without truncation', () => {
  render({ ...approval, parameters: Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`field${i}`, `value${i}`])), technical_detail: 'Requested by the owner' })
  expect(container.textContent).toContain('Solo esta acción')
  expect(container.textContent).toContain('write_file')
  expect(container.textContent).toContain('value11')
  expect(container.textContent).toContain('Requested by the owner')
  expect(container.querySelector('[role="alertdialog"]')).toBeNull()
})

it('submits once under double clicks and never calls approval execution complete', async () => {
  render()
  await act(async () => { button('Permitir una vez').click(); button('Permitir una vez').click() })
  expect(resolve).toHaveBeenCalledTimes(1)
  expect(resolve).toHaveBeenCalledWith('proposal-1', 'once', { totp: null })
  expect(onResolved).toHaveBeenCalledTimes(1)
  expect(success.mock.calls[0][0].title).not.toMatch(/ejecutada/)
  expect(container.textContent).toContain('Decisión registrada')
})

it('keeps MFA in place during submission and refocuses the same field after rejection', async () => {
  let reject!: (reason: unknown) => void
  resolve.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  render({ ...approval, required_level: 'mfa', mfa_enrolled: true })
  act(() => button('Permitir una vez').click())
  expect(resolve).not.toHaveBeenCalled()
  const input = document.querySelector('input')!
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '123456')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => button('Confirmar con código').click())
  expect(resolve).toHaveBeenCalledWith('proposal-1', 'once', { totp: '123456' })
  expect(document.querySelector('input')).toBe(input)
  expect(input.disabled).toBe(true)
  await act(async () => button('Confirmar con código').click())
  expect(resolve).toHaveBeenCalledTimes(1)
  await act(async () => reject(new ApiError('Invalid code', 401, { detail: { code: 'invalid_totp' } })))
  expect(document.querySelector('input')).toBe(input)
  expect(input.disabled).toBe(false)
  expect(document.activeElement).toBe(input)
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('Código incorrecto')
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  expect(resolve).toHaveBeenCalledTimes(1)
})

it('cannot approve enterprise-routed requests but can deny them', async () => {
  render({ ...approval, route: 'enterprise' })
  expect(container.textContent).toContain('aprobación de tu organización')
  expect(container.textContent).not.toContain('Permitir una vez')
  await act(async () => button('Rechazar').click())
  expect(resolve).toHaveBeenCalledWith('proposal-1', 'deny', undefined)
})

it('does not downgrade missing classification to a simple approval', () => {
  render({ ...approval, required_level: undefined })
  act(() => button('Permitir una vez').click())
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  expect(resolve).not.toHaveBeenCalled()
})

it('recognizes an expired request from the structured code, not translated copy', async () => {
  resolve.mockRejectedValue(new ApiError('Ya no es válida', 400, { detail: { code: 'proposal_invalid' } }))
  render()
  await act(async () => button('Permitir una vez').click())
  expect(container.textContent).toContain('Esta solicitud caducó')
  expect(container.textContent).not.toContain('Permitir una vez')
})
