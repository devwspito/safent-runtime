import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { usePendingApprovals } from './usePendingApprovals'
const { list } = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('../api/client', () => ({ listPendingApprovals: list }))

let root: Root
let container: HTMLDivElement
function Probe() {
  const { approvals, error } = usePendingApprovals(3000)
  return <div>{error ? 'unavailable' : 'ready'}:{approvals.map(a => a.proposal_id).join(',')}</div>
}
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.useFakeTimers()
  list.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('preserves server-pending requests regardless of browser age assumptions', async () => {
  list.mockResolvedValue([{ proposal_id: 'old-but-pending', created_at: '2020-01-01T00:00:00Z' }])
  await act(async () => root.render(<Probe />))
  expect(container.textContent).toBe('ready:old-but-pending')
})

it('retains the queue and exposes a transient error instead of claiming nothing is pending', async () => {
  list.mockResolvedValueOnce([{ proposal_id: 'pending' }]).mockRejectedValueOnce(new Error('503'))
  await act(async () => root.render(<Probe />))
  await act(async () => vi.advanceTimersByTime(3000))
  expect(container.textContent).toBe('unavailable:pending')
})

it('does not start overlapping requests when a poll is slow', async () => {
  list.mockReturnValue(new Promise(() => {}))
  await act(async () => root.render(<Probe />))
  await act(async () => vi.advanceTimersByTime(12000))
  expect(list).toHaveBeenCalledTimes(1)
})
