import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import KillSwitchBanner from './KillSwitchBanner'
import { getKillSwitch } from '../api/client'

vi.mock('../api/client', () => ({ getKillSwitch: vi.fn() }))
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers() })
async function render() { await act(async () => { root.render(<MemoryRouter><KillSwitchBanner /></MemoryRouter>) }) }

it('handles unavailable initial state and recovers on the next poll', async () => {
  vi.mocked(getKillSwitch).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ engaged: false, reason: null, changed_at: null, changed_by: null })
  await render()
  expect(host.textContent).toContain('No se puede verificar')
  expect(host.textContent).not.toContain('Liberar')
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(host.textContent).toBe('')
})

it('does not erase the last confirmed brake after a failed refresh', async () => {
  vi.mocked(getKillSwitch).mockResolvedValueOnce({ engaged: true, reason: null, changed_at: null, changed_by: null })
    .mockRejectedValue(new Error('offline'))
  await render()
  expect(host.textContent).toContain('ACTIVADO')
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(host.textContent).toContain('último estado confirmado era activado')
  expect(host.textContent).not.toContain('el agente no ejecuta nada')
})

it('does not overlap slow polling requests', async () => {
  vi.mocked(getKillSwitch).mockReturnValue(new Promise(() => {}))
  await render()
  await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
  expect(getKillSwitch).toHaveBeenCalledTimes(1)
})
