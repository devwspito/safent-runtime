// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { diagnosticsAction } from './diagnostics-action.js'
import { requestDiagnostics } from './ipc.js'

let button: HTMLButtonElement
let note: HTMLElement
beforeEach(() => { button=document.createElement('button'); note=document.createElement('p') })
afterEach(() => vi.unstubAllGlobals())

it('only announces success after native confirms the saved file', async () => {
  let resolve!: (value: {status:'saved'}) => void
  const invoke=vi.fn(()=>new Promise<{status:'saved'}>(yes=>{resolve=yes}))
  const run=diagnosticsAction(button,note,invoke)
  const first=run(); await run()
  expect(invoke).toHaveBeenCalledTimes(1)
  expect(button.disabled).toBe(true)
  expect(note.textContent).not.toContain('guardado en')
  resolve({status:'saved'}); await first
  expect(note.textContent).toContain('guardado en la ubicación elegida')
  expect(button.disabled).toBe(false)
  expect(button.hasAttribute('aria-busy')).toBe(false)
})
it('cancel is a normal outcome and allows another attempt', async () => {
  const invoke=vi.fn().mockResolvedValue({status:'cancelled'})
  const run=diagnosticsAction(button,note,invoke); await run(); await run()
  expect(note.getAttribute('role')).toBe('status')
  expect(note.textContent).toContain('No se ha guardado')
  expect(invoke).toHaveBeenCalledTimes(2)
})
it('failure is recoverable and never exposes raw native error text', async () => {
  const invoke=vi.fn().mockRejectedValue(new Error('/Users/private/password=SECRET'))
  const run=diagnosticsAction(button,note,invoke); await run()
  expect(note.getAttribute('role')).toBe('alert')
  expect(note.textContent).not.toContain('SECRET')
  expect(button.disabled).toBe(false)
  invoke.mockResolvedValue({status:'saved'}); await run()
  expect(note.getAttribute('role')).toBe('status')
})
it('browser preview cannot report fake export success', async () => {
  await expect(requestDiagnostics()).rejects.toThrow('Native Safent runtime is unavailable')
})
it('IPC does not accept a caller path and rejects unknown or empty result', async () => {
  const invoke=vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  await expect(requestDiagnostics()).rejects.toThrow('Unconfirmed')
  expect(invoke).toHaveBeenCalledWith('export_diagnostics')
  invoke.mockResolvedValue({status:'cancelled'})
  await expect(requestDiagnostics()).resolves.toEqual({status:'cancelled'})
})
