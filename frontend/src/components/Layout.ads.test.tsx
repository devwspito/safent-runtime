import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Layout from './Layout'
import AdsView from '../views/AdsView'
import { I18nProvider } from '../lib/i18n'
import type { AdsAvailability } from '../hooks/useAdsAvailability'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

const state = vi.hoisted(() => ({
  availability: { status: 'ready', reason: null, refresh: vi.fn() } as AdsAvailability,
  chat: {
    convId: null, agentId: null, messages: [], status: { phase: 'idle' },
    sendMessage: vi.fn(), startNew: vi.fn(), startNewWithAgent: vi.fn(),
    loadConversation: vi.fn(), stopStream: vi.fn(), conversationsTick: 0,
  },
}))
vi.mock('../hooks/useAdsAvailability', () => ({ useAdsAvailability: () => state.availability }))
vi.mock('../hooks/useChat', () => ({ useChat: () => state.chat }))
vi.mock('../hooks/useFeatures', () => ({ useFeatures: () => ({ allowed: () => true, isLoading: false }) }))
vi.mock('../hooks/usePendingApprovals', () => ({ usePendingApprovals: () => ({ approvals: [] }) }))
vi.mock('../hooks/usePendingInboundDelegations', () => ({ usePendingInboundDelegations: () => [] }))
vi.mock('../api/client', () => ({ listConversations: vi.fn().mockResolvedValue([]) }))
vi.mock('./NotificationsPanel', () => ({ default: () => null }))
vi.mock('./KillSwitchBanner', () => ({ default: () => <div>Freno de emergencia</div> }))
vi.mock('./SystemUpdateFooter', () => ({ SystemUpdateFooter: () => null }))
vi.mock('./CompanionInstallAction', () => ({ CompanionInstallAction: () => null }))
vi.mock('../views/sectionHubIds', () => ({ CAPACIDADES_VIEW_IDS: [], SISTEMA_VIEW_IDS: [] }))

let host: HTMLDivElement
let root: Root
let navigate: ReturnType<typeof useNavigate>
function LocationProbe() {
  navigate = useNavigate()
  const location = useLocation()
  return <output data-location>{location.pathname + location.search}</output>
}
async function render(initial = '/tareas?tab=programadas') {
  await act(async () => root.render(<I18nProvider><MemoryRouter initialEntries={[initial]}>
    <LocationProbe />
    <Routes><Route element={<Layout activeProviderReload={() => {}} />}>
      <Route path="anuncios" element={<AdsView />} />
      <Route path="*" element={<h1>Safent principal</h1>} />
    </Route></Routes>
  </MemoryRouter></I18nProvider>))
}
function sidebar() { return host.querySelector<HTMLElement>('#community-sidebar')! }
function backButton() {
  return [...host.querySelectorAll('button')].find(button => button.textContent?.includes('Volver a Safent'))!
}
beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  state.availability = { status: 'ready', reason: null, refresh: vi.fn() }
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

describe('local Ads owns the navigation workspace', () => {
  it('hides, rather than remounts, the global sidebar; returns to the prior route and focuses Ads', async () => {
    await render()
    const originalSidebar = sidebar()
    const adsLink = sidebar().querySelector<HTMLAnchorElement>('a[href="/anuncios"]')!
    await act(async () => adsLink.click())
    expect(sidebar()).toBe(originalSidebar)
    expect(sidebar().hidden).toBe(true)
    expect(host.querySelector('button[aria-controls="community-sidebar"][aria-expanded="false"]')).toBeNull()
    expect(host.querySelector('iframe')?.getAttribute('src')).toBe('/ads/')
    expect(host.textContent).toContain('Freno de emergencia')
    expect(document.activeElement).toBe(backButton())
    await act(async () => backButton().click())
    expect(host.querySelector('[data-location]')?.textContent).toBe('/tareas?tab=programadas')
    expect(sidebar().hidden).toBe(false)
    expect(sidebar()).toBe(originalSidebar)
    expect(host.querySelector('iframe')).toBeNull()
    expect(document.activeElement).toBe(adsLink)
    expect(state.chat.startNew).not.toHaveBeenCalled()
  })

  it('preserves a collapsed sidebar and restores its visible reopen button on return', async () => {
    await render()
    await act(async () => host.querySelector<HTMLButtonElement>('button[aria-controls="community-sidebar"]')!.click())
    await act(async () => navigate('/anuncios'))
    expect(sidebar().hidden).toBe(true)
    await act(async () => backButton().click())
    const reopen = host.querySelector<HTMLButtonElement>('button[aria-expanded="false"][aria-controls="community-sidebar"]')!
    expect(sidebar().hidden).toBe(true)
    expect(reopen).not.toBeNull()
    expect(document.activeElement).toBe(reopen)
  })

  it.each(['/anuncios', '/anuncios/'])('supports direct entry at %s on mobile without an overlapping global drawer', async path => {
    vi.mocked(window.matchMedia).mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    await render(path)
    expect(host.querySelector('[data-ads-workspace="true"]')?.getAttribute('data-sidebar-open')).toBe('false')
    expect(document.activeElement).toBe(backButton())
    await act(async () => backButton().click())
    expect(host.querySelector('[data-location]')?.textContent).toBe('/chat')
    expect(sidebar().hidden).toBe(true)
    expect(document.activeElement?.getAttribute('aria-controls')).toBe('community-sidebar')
  })

  it('restores navigation on router Back, not only the explicit return action', async () => {
    await render()
    await act(async () => navigate('/anuncios'))
    await act(async () => navigate(-1))
    expect(sidebar().hidden).toBe(false)
    expect(host.querySelector('iframe')).toBeNull()
    expect(document.activeElement?.getAttribute('href')).toBe('/anuncios')
  })

  it.each(['loading', 'managed', 'unavailable'] as const)('keeps the shell for %s; removes a previously mounted local panel', async status => {
    await render('/anuncios')
    expect(sidebar().hidden).toBe(true)
    state.availability = { status, reason: status === 'unavailable' ? 'not_installed' : null, refresh: vi.fn() }
    await render('/anuncios')
    expect(sidebar().hidden).toBe(false)
    expect(host.querySelector('iframe')).toBeNull()
    expect(backButton()).toBeUndefined()
  })

  it('gives no-accounts onboarding the same single-sidebar space, without inventing a connection', async () => {
    state.availability = { status: 'unavailable', reason: 'no_accounts', refresh: vi.fn() }
    await render('/anuncios')
    expect(sidebar().hidden).toBe(true)
    expect(host.querySelector('iframe')).not.toBeNull()
    expect(host.textContent).toContain('Conecta tus cuentas de Google o Meta')
  })
})
