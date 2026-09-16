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

it('does not request a retry for a non-retryable machine failure, even from a stale click', () => {
  snapshot(1, { kind: 'failed', code: 'machine_start_failed', retryable: false })
  const retry = byId('btn-retry')
  expect(retry.hidden).toBe(true)
  expect(retry.disabled).toBe(true)
  expect(byId('failed-hint').textContent).not.toMatch(/vuelve a intentarlo/i)
  retry.disabled = false // A queued/synthetic event is not lifecycle authority.
  retry.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  expect(ipc.retry).not.toHaveBeenCalled()
  expect(byId('screen-failed').hidden).toBe(false)
})

it('keeps private runtime preparation in product language and blocked errors in diagnostic details', () => {
  snapshot(1, { kind: 'stage', stage: 'machine' })
  expect(byId('preparing-status').textContent).toBe('Preparando el espacio seguro')
  snapshot(1, { kind: 'stage', stage: 'pull_engine' })
  snapshot(1, { kind: 'progress', stage: 'pull_engine', done: 3, total: 5, unit: 'layers' })
  expect(byId('preparing-stages').textContent).toContain('3 de 5 partes')
  expect(byId('screen-preparing').textContent).not.toMatch(/podman|docker|capas|máquina virtual|terminal/i)
  snapshot(1, { kind: 'failed', code: 'local_storage_conflict', retryable: false })
  expect(byId('btn-retry').hidden).toBe(true)
  expect(byId('failed-hint').textContent).not.toMatch(/vuelve a intentarlo/i)
  expect(byId('failed-code').textContent).toBe('local_storage_conflict')
  expect(document.querySelector('details')?.open).toBe(false)
  expect(ipc.cancel).not.toHaveBeenCalled()
  expect(ipc.retry).not.toHaveBeenCalled()
})
