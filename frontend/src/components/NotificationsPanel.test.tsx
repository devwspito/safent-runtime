import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { Notification } from '../api/types'

const api = vi.hoisted(() => ({ listNotifications: vi.fn(), getUnreadCount: vi.fn(), markNotificationRead: vi.fn(), markAllNotificationsRead: vi.fn() }))
vi.mock('../api/client', () => api)
import NotificationsPanel from './NotificationsPanel'

const notification = { id: 'notice-1', kind: 'system', title: 'Revisión disponible', body: 'Revisa la propuesta antes de aprobar.', status: 'info', conversation_id: 'thread-1', created_at: '2026-09-12T10:00:00Z', read: false } as Notification
const loadConversation = vi.fn()
let container: HTMLDivElement
let root: Root
function button(label: string) { return [...document.querySelectorAll('button')].find(value => value.getAttribute('aria-label')?.includes(label) || value.textContent?.includes(label))! }
async function click(target: HTMLElement) { await act(async () => target.click()) }
async function mount() { await act(async () => root.render(<MemoryRouter><NotificationsPanel loadConversation={loadConversation} /></MemoryRouter>)) }
async function open() { await click(container.querySelector('button')!) }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(api).forEach(mock => mock.mockReset())
  api.listNotifications.mockResolvedValue([notification])
  api.getUnreadCount.mockResolvedValue({ count: 1 })
  api.markNotificationRead.mockResolvedValue({})
  api.markAllNotificationsRead.mockResolvedValue({})
  loadConversation.mockReset().mockResolvedValue(undefined)
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })

it('shows a recoverable failure rather than an empty inbox or a zero count', async () => {
  api.listNotifications.mockRejectedValueOnce(new Error('private')).mockResolvedValueOnce([])
  api.getUnreadCount.mockRejectedValue(new Error('private'))
  await mount(); await open()
  expect(document.body.textContent).toContain('No se pudieron consultar')
  expect(document.body.textContent).not.toContain('No hay notificaciones')
  expect(container.querySelector('button')?.ariaLabel).toContain('recuento no disponible')
  await click(button('Reintentar'))
  expect(document.body.textContent).toContain('No hay notificaciones')
})
it('does not mark a notification read or open its conversation when the server rejects the write', async () => {
  api.markNotificationRead.mockRejectedValue(new Error('offline'))
  await mount(); await open(); await click(button(notification.title))
  expect(document.body.textContent).toContain('No se pudo confirmar la lectura')
  expect(button(notification.title).dataset.unread).toBe('true')
  expect(loadConversation).not.toHaveBeenCalled()
  expect(container.querySelector('button')?.ariaLabel).toContain('1 notificaciones')
})
it('waits for confirmed read state, prevents double submission and reports conversation failure separately', async () => {
  let finish!: () => void
  api.markNotificationRead.mockReturnValue(new Promise<void>(resolve => { finish = resolve }))
  loadConversation.mockRejectedValue(new Error('not found'))
  await mount(); await open(); await click(button(notification.title)); await click(button(notification.title))
  expect(api.markNotificationRead).toHaveBeenCalledTimes(1)
  expect(button(notification.title).dataset.unread).toBe('true')
  await act(async () => finish())
  expect(button(notification.title).dataset.unread).toBe('false')
  expect(document.body.textContent).toContain('No se pudo abrir la conversación')
})
it('ignores a response from a closed panel after a new opening has loaded', async () => {
  let finish!: (values: Notification[]) => void
  api.listNotifications.mockReturnValueOnce(new Promise(resolve => { finish = resolve })).mockResolvedValueOnce([])
  await mount(); await open(); await click(button('Cerrar')); await open()
  await act(async () => finish([notification]))
  expect(document.body.textContent).toContain('No hay notificaciones')
  expect(document.body.textContent).not.toContain(notification.title)
})
it('does not navigate from a late read confirmation after the panel closes', async () => {
  let finish!: () => void
  api.markNotificationRead.mockReturnValue(new Promise<void>(resolve => { finish = resolve }))
  await mount(); await open(); await click(button(notification.title)); await click(button('Cerrar'))
  await act(async () => finish())
  expect(loadConversation).not.toHaveBeenCalled()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})
it('preserves unread state on a failed bulk read and supports Escape back to the bell', async () => {
  api.markAllNotificationsRead.mockRejectedValue(new Error('offline'))
  await mount(); await open(); await click(button('Marcar todas'))
  expect(button(notification.title).dataset.unread).toBe('true')
  await act(async () => document.querySelector('[role="dialog"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(container.querySelector('button'))
})
