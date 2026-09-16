import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ listProviders: vi.fn(), listNativeProviders: vi.fn(), getNativeActive: vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('sileo', () => ({ sileo: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
import ProvidersView from './ProvidersView'

const custom = { provider_id: 'custom-id', alias: 'My Qwen', kind: 'openai_compatible', default_model: 'qwen3.8-27b', base_url: 'https://model.test/v1', is_active: true }
const codex = { provider_id: 'openai-codex', alias: 'OpenAI Codex', kind: 'openai-codex', default_model: 'gpt-6-astra', is_active: true }
let host: HTMLDivElement, root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  Object.values(api).forEach(f => f.mockReset())
  api.listProviders.mockResolvedValue([custom]); api.listNativeProviders.mockResolvedValue([])
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })
async function render() { await act(async () => root.render(<ProvidersView />)) }
function rows() { return [...host.querySelectorAll('li')] }
it('shows only native Codex active despite stale SQL Qwen and restores Qwen activation control', async () => {
  api.getNativeActive.mockResolvedValue(codex)
  await render()
  expect(rows()).toHaveLength(2)
  const qwen = rows().find(r => r.textContent?.includes('My Qwen'))!
  const selected = rows().find(r => r.textContent?.includes('OpenAI Codex'))!
  expect(selected.textContent).toContain('Activo')
  expect(qwen.textContent).not.toContain('Activo')
  expect([...qwen.querySelectorAll('button')].some(b => b.textContent === 'Activar')).toBe(true)
})
it('collapses the native custom mirror without displaying two active cards', async () => {
  api.getNativeActive.mockResolvedValue({ ...custom, provider_id: 'custom', alias: 'Custom' })
  await render()
  expect(rows()).toHaveLength(1)
  expect(rows()[0].textContent).toContain('My Qwen')
  expect(rows()[0].textContent).toContain('Activo')
})
it('does not call a stale saved row active when Hermes has no selected model', async () => {
  api.getNativeActive.mockResolvedValue(null)
  await render()
  expect(rows()[0].textContent).not.toContain('Activo')
})
it('shows a retryable load error instead of claiming no active model when native lookup fails', async () => {
  api.getNativeActive.mockRejectedValue(new Error('private daemon details'))
  await render()
  expect(host.querySelector('[role="alert"]')).toBeTruthy()
  expect(host.textContent).not.toContain('private daemon details')
  expect(rows()).toHaveLength(0)
})
