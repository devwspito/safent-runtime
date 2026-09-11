import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import Layout from '../components/Layout'
import ChatView from './ChatView'
import { I18nProvider } from '../lib/i18n'
import { uploadWorkspaceFile } from '../api/client'

const chat = vi.hoisted(() => ({
  convId: 'thread-a' as string | null, agentId: null as string | null,
  messages: [], status: { phase: 'idle' } as { phase: 'idle' | 'streaming' },
  sendMessage: vi.fn().mockResolvedValue(undefined), startNew: vi.fn(), startNewWithAgent: vi.fn(),
  loadConversation: vi.fn(), stopStream: vi.fn(), conversationsTick: 0,
  reconnecting: false, liveBrowserActive: false,
}))
vi.mock('../hooks/useChat', () => ({ useChat: () => chat }))
vi.mock('../hooks/useFeatures', () => ({ useFeatures: () => ({ allowed: () => true, isLoading: false }) }))
vi.mock('../hooks/useAdsAvailability', () => ({ useAdsAvailability: () => ({ status: 'unavailable', reason: 'no_accounts' }) }))
vi.mock('../hooks/usePendingApprovals', () => ({ usePendingApprovals: () => ({ approvals: [] }) }))
vi.mock('../hooks/usePendingInboundDelegations', () => ({ usePendingInboundDelegations: () => [] }))
vi.mock('../components/NotificationsPanel', () => ({ default: () => null }))
vi.mock('../components/KillSwitchBanner', () => ({ default: () => null }))
vi.mock('../components/SystemUpdateFooter', () => ({ SystemUpdateFooter: () => null }))
vi.mock('../components/PendingApprovalsInChat', () => ({ default: () => null }))
vi.mock('../components/ContextPanel', () => ({ default: () => null }))
vi.mock('../components/VncView', () => ({ VncFrame: () => null }))
vi.mock('./sectionHubIds', () => ({ CAPACIDADES_VIEW_IDS: [], SISTEMA_VIEW_IDS: [] }))
vi.mock('../api/client', () => ({
  listConversations: vi.fn().mockResolvedValue([]), listProviders: vi.fn().mockResolvedValue([]),
  listSkills: vi.fn().mockResolvedValue([]), uploadWorkspaceFile: vi.fn(), ApiError: class extends Error {},
}))

let host: HTMLDivElement
let root: Root
async function render(showChat = true) {
  await act(async () => { root.render(<I18nProvider><MemoryRouter><Routes>
    <Route element={<Layout activeProviderReload={() => {}} />}><Route index element={showChat ? <ChatView /> : <div>Otra vista</div>} /></Route>
  </Routes></MemoryRouter></I18nProvider>) })
}
async function type(text: string) {
  const field = host.querySelector('textarea')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(field, text)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks(); chat.convId = 'thread-a'; chat.agentId = null; chat.status = { phase: 'idle' }
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('isolates drafts and an upload completing after navigation in the real Layout/ChatView/Composer', async () => {
  let finishUpload!: (value: Awaited<ReturnType<typeof uploadWorkspaceFile>>) => void
  vi.mocked(uploadWorkspaceFile).mockReturnValue(new Promise(resolve => { finishUpload = resolve }))
  await render()
  await type('Borrador del hilo A')
  const file = host.querySelector<HTMLInputElement>('input[type=file]')!
  Object.defineProperty(file, 'files', { value: [new File(['brand'], 'brief-a.txt')] })
  await act(async () => { file.dispatchEvent(new Event('change', { bubbles: true })) })
  chat.convId = 'thread-b'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('')
  expect(host.textContent).not.toContain('brief-a.txt')
  await act(async () => { finishUpload({ path: '/workspace/brief-a.txt', name: 'brief-a.txt', size: 5 }) })
  expect(host.textContent).not.toContain('brief-a.txt')
  await type('Mensaje del hilo B')
  await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-label="Enviar mensaje (Enter)"]')!.click() })
  expect(chat.sendMessage).toHaveBeenCalledExactlyOnceWith('Mensaje del hilo B')
  chat.convId = 'thread-a'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('Borrador del hilo A')
  expect(host.textContent).toContain('brief-a.txt')
})

it('keeps drafts separate while a thread is streaming and across leaving the chat route', async () => {
  chat.status = { phase: 'streaming' }
  await render()
  await type('Siguiente mensaje de A')
  chat.convId = 'thread-b'; chat.status = { phase: 'idle' }
  await render()
  expect(host.querySelector('textarea')!.value).toBe('')
  await type('Borrador de B')
  await render(false)
  expect(host.querySelector('textarea')).toBeNull()
  await render()
  expect(host.querySelector('textarea')!.value).toBe('Borrador de B')
  chat.convId = 'thread-a'; chat.status = { phase: 'streaming' }
  await render()
  expect(host.querySelector('textarea')!.value).toBe('Siguiente mensaje de A')
  expect(chat.sendMessage).not.toHaveBeenCalled()
})

it('separates unsent drafts by agent and does not inherit a previous agent for historical threads', async () => {
  chat.convId = null; chat.agentId = 'agent-a'
  await render()
  await type('Borrador para agente A')
  chat.agentId = 'agent-b'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('')
  await type('Borrador para agente B')
  chat.agentId = 'agent-a'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('Borrador para agente A')
  chat.convId = 'historical'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('')
  await type('Borrador del histórico')
  // useChat currently retains the previous agent on a historical load. It must
  // neither import that agent's draft nor hide the existing historical draft.
  chat.agentId = 'agent-b'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('Borrador del histórico')
})

it('binds a newly sent draft to its generated conversation id without carrying it into the next new chat', async () => {
  chat.convId = null; chat.agentId = 'agent-a'
  await render()
  await type('Primer mensaje')
  await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-label="Enviar mensaje (Enter)"]')!.click() })
  chat.convId = 'generated-thread'; chat.status = { phase: 'streaming' }
  await render()
  await type('Segundo mensaje todavía sin enviar')
  chat.convId = null; chat.status = { phase: 'idle' }
  await render()
  expect(host.querySelector('textarea')!.value).toBe('')
  chat.convId = 'generated-thread'
  await render()
  expect(host.querySelector('textarea')!.value).toBe('Segundo mensaje todavía sin enviar')
  expect(chat.sendMessage).toHaveBeenCalledExactlyOnceWith('Primer mensaje')
})
