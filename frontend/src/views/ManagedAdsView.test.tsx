import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ManagedAdsView } from './ManagedAdsView'
import { adsPolicyFixture as policy, adsBindingFixture as binding, adsCampaignFixture as campaign, adsProposalFixture as proposal, adsReviewFixture as review } from '../api/managedAds.fixtures'
import type { AdsPolicy } from '../api/managedAds'
const { list, metrics, propose, prepare } = vi.hoisted(() => ({ list: vi.fn(), metrics: vi.fn(), propose: vi.fn(), prepare: vi.fn() }))
vi.mock('../api/managedAds', async importOriginal => ({ ...await importOriginal<typeof import('../api/managedAds')>(),
  listManagedCampaigns: list, getManagedMetrics: metrics, proposeManagedChange: propose, prepareManagedReview: prepare,
}))
let node: HTMLDivElement, root: Root
const second = { ...binding, grant_id: 'grant-two', connection_id: '55555555-5555-4555-8555-555555555555' }
beforeEach(() => {
  vi.clearAllMocks()
  list.mockResolvedValue({ items: [campaign], cursor: null })
  metrics.mockResolvedValue({ entity_ref: campaign.entity_ref, points: [], granularity: 'daily' })
  propose.mockResolvedValue(proposal)
  prepare.mockResolvedValue(review)
  node = document.createElement('div'); document.body.appendChild(node); root = createRoot(node)
})
afterEach(() => { act(() => root.unmount()); node.remove(); vi.useRealTimers() })
const render = async (value: AdsPolicy = policy) => act(async () => root.render(<ManagedAdsView key={JSON.stringify(value)} policy={value} refresh={vi.fn()} refreshing={false} />))
const button = (text: string) => [...node.querySelectorAll('button')].find(item => item.textContent?.includes(text))!
const click = async (text: string) => act(async () => button(text).click())
async function select(value = binding.grant_id) {
  await act(async () => {
    const input = node.querySelector('select')!
    input.value = value; input.dispatchEvent(new Event('change', { bubbles: true }))
  })
}
async function openCampaign() { await render(); await select(); await click('Consultar campañas'); await click(campaign.name) }
async function fill() {
  await act(async () => {
    const input = node.querySelector('input')!
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '10.00')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    const textarea = node.querySelector('textarea')!
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, 'Ajuste revisado por Luis')
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
it('requires explicit assignment, distinguishes two OAuth routes to the same account', async () => {
  await render({ ...policy, bindings: [binding, second] })
  expect(list).not.toHaveBeenCalled()
  expect(node.querySelector('select')?.value).toBe('')
  expect(node.textContent).toContain(binding.connection_id)
  expect(node.textContent).toContain(second.connection_id)
  await select(second.grant_id); await click('Consultar campañas')
  expect(list.mock.calls[0][1]).toEqual(second)
})
it('renders empty managed state without local iframe, install or approval controls', async () => {
  await render({ ...policy, bindings: [] })
  expect(node.textContent).toContain('No hay asignaciones')
  expect(node.querySelector('iframe')).toBeNull()
  expect(node.textContent).not.toContain('Instalar')
  expect(node.querySelector('select')?.disabled).toBe(true)
})
it('saves one exact proposal and separately prepares a non-approving Enterprise link', async () => {
  await openCampaign(); await fill()
  await act(async () => { button('Guardar propuesta').click(); button('Guardar propuesta').click() })
  expect(propose).toHaveBeenCalledTimes(1)
  expect(propose.mock.calls[0].slice(1, 6)).toEqual([binding, campaign, 'budget', '10.00', 'Ajuste revisado por Luis'])
  expect(node.textContent).toContain('sin ejecutar')
  expect(prepare).not.toHaveBeenCalled()
  await click('Preparar revisión')
  expect(prepare).toHaveBeenCalledTimes(1)
  expect(node.querySelector('a')?.getAttribute('href')).toBe(review.review_url)
  expect(node.querySelector('a')?.getAttribute('rel')).toBe('noopener noreferrer')
  expect(node.textContent).not.toContain('TOTP')
  expect(node.querySelector('iframe')).toBeNull()
})
it('switching accounts aborts and ignores a late response; does not merge portfolios', async () => {
  let finish!: (value: unknown) => void
  list.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  await render({ ...policy, bindings: [binding, second] }); await select(); await click('Consultar campañas')
  const firstSignal = list.mock.calls[0][3] as AbortSignal
  await select(second.grant_id)
  expect(firstSignal.aborted).toBe(true)
  await act(async () => finish({ items: [campaign], cursor: null }))
  expect(node.textContent).not.toContain(campaign.name)
})
it('new policy revision clears campaign, draft, selection and any old review immediately', async () => {
  await openCampaign(); await fill(); await click('Guardar propuesta'); await click('Preparar revisión')
  await render({ ...policy, bindings: [{ ...binding, revision: 2 }] })
  expect(node.querySelector('select')?.value).toBe('')
  expect(node.querySelector('a')).toBeNull()
  expect(node.textContent).not.toContain(campaign.name)
})
it('revoked assignment disappears, with no local fallback', async () => {
  await openCampaign(); await render({ ...policy, bindings: [] })
  expect(node.textContent).not.toContain(campaign.name)
  expect(node.querySelector('iframe')).toBeNull()
})
it('failed metrics admission hides previously loaded campaign and shows a sanitized error', async () => {
  await openCampaign()
  metrics.mockRejectedValueOnce(new Error('secret-upstream-message'))
  await click('Consultar métricas')
  expect(node.textContent).toContain('Se han ocultado')
  expect(node.textContent).not.toContain('secret-upstream-message')
  expect(node.textContent).not.toContain(campaign.name)
  expect(node.querySelector('form')).toBeNull()
})
it('ambiguous proposal result is not automatically retried or shown as rolled back', async () => {
  await openCampaign(); await fill()
  propose.mockRejectedValueOnce(new Error('timeout'))
  await click('Guardar propuesta')
  expect(node.textContent).toContain('podría haberse registrado')
  expect(propose).toHaveBeenCalledTimes(1)
  expect(node.querySelector('form')).toBeNull()
})
it('review expiry removes link without requesting a new intent automatically', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-12T12:00:00Z'))
  prepare.mockResolvedValueOnce({ ...review, expires_at: '2026-09-12T12:00:01Z' })
  await openCampaign(); await fill(); await click('Guardar propuesta'); await click('Preparar revisión')
  expect(node.querySelector('a')).not.toBeNull()
  await act(async () => vi.advanceTimersByTime(2000))
  expect(node.querySelector('a')).toBeNull()
  expect(node.textContent).toContain('ha caducado')
  expect(prepare).toHaveBeenCalledTimes(1)
})
it('uncontrollable campaign allows metrics but no proposal form', async () => {
  list.mockResolvedValueOnce({ items: [{ ...campaign, is_controllable: false }], cursor: null })
  await openCampaign()
  expect(node.querySelector('form')).toBeNull()
  expect(button('Consultar métricas')).toBeDefined()
})
