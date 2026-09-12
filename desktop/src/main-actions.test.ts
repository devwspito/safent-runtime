// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { BootstrapSnapshot } from './bootstrap-state.js'

const ipc = vi.hoisted(() => ({ cancel: vi.fn(), retry: vi.fn(), subscribe: vi.fn() }))
vi.mock('./ipc.js', () => ({
  isTauriRuntime: () => true, requestCancel: ipc.cancel, requestRetry: ipc.retry,
  requestDiagnostics: vi.fn(), subscribeToBootstrapState: ipc.subscribe,
}))
let receive!: (snapshot: BootstrapSnapshot) => void
let sequence: number
function snapshot(attempt: number, event: BootstrapSnapshot['event'], irreversible = false) {
  receive({ sequence: ++sequence, attempt_id: attempt, last_stage: 'preflight', point_of_no_return: irreversible, event })
}
const byId = (id: string) => document.getElementById(id) as HTMLButtonElement
beforeEach(async () => {
  vi.resetModules(); vi.clearAllMocks(); sequence = 0
  document.documentElement.innerHTML = readFileSync(`${process.cwd()}/src/index.html`, 'utf8')
  ipc.subscribe.mockImplementation(async callback => { receive = callback; return () => {} })
  await import('./main.js')
})
afterEach(() => { document.body.innerHTML = '' })

it('wires real DOM cancel → acknowledgement → terminal → retry without stale cancellation feedback', async () => {
  let finish!: () => void
  ipc.cancel.mockReturnValueOnce(new Promise<void>(yes => { finish = yes }))
  snapshot(1, { kind: 'stage', stage: 'preflight' })
  const cancel = byId('btn-cancel')
  cancel.focus(); cancel.click(); cancel.click()
  expect(ipc.cancel).toHaveBeenCalledExactlyOnceWith(1)
  expect(cancel.disabled).toBe(true)
  expect(document.activeElement).toBe(byId('cancel-note'))
  finish(); await Promise.resolve(); await Promise.resolve()
  expect(byId('cancel-note').textContent).toContain('Cancelación solicitada')
  snapshot(1, { kind: 'failed', code: 'cancelled_by_owner', retryable: true })
  expect(byId('screen-failed').hidden).toBe(false)
  ipc.retry.mockResolvedValueOnce(undefined)
  byId('btn-retry').click()
  expect(ipc.retry).toHaveBeenCalledExactlyOnceWith(1)
  snapshot(2, { kind: 'stage', stage: 'preflight' })
  expect(byId('btn-cancel').disabled).toBe(false)
  expect(byId('cancel-note').hidden).toBe(true)
  expect(document.activeElement).toBe(byId('preparing-heading'))
})

it('does not show late IPC failure in a replacement attempt or override its irreversible stage', async () => {
  let fail!: (reason: Error) => void
  ipc.cancel.mockReturnValueOnce(new Promise((_yes, no) => { fail = no }))
  snapshot(1, { kind: 'stage', stage: 'preflight' })
  byId('btn-cancel').click()
  snapshot(2, { kind: 'stage', stage: 'container' }, true)
  fail(new Error('old failure')); await Promise.resolve(); await Promise.resolve()
  expect(byId('btn-cancel').disabled).toBe(true)
  expect(byId('cancel-note').textContent).toContain('ya no se puede cancelar')
  expect(byId('action-error').hidden).toBe(true)
})
