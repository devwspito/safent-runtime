import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const { auth } = vi.hoisted(() => ({ auth: vi.fn(() => ({ kind: 'authenticated' })) }))
vi.mock('../lib/token', () => ({ token: () => 'synthetic-local-owner', getAuthStatus: auth }))
import { getAdsPolicy, listManagedCampaigns, prepareManagedReview, proposeManagedChange, getManagedMetrics } from './managedAds'
import { adsBindingFixture as binding, adsPolicyFixture as policy, adsCampaignFixture as campaign, adsProposalFixture as proposal, adsReviewFixture as review } from './managedAds.fixtures'
const signal = () => new AbortController().signal
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const policyResponse = () => response({ policy })
beforeEach(() => auth.mockReturnValue({ kind: 'authenticated' }))
afterEach(() => vi.unstubAllGlobals())

it('transports only local owner auth and exact public snapshot, never retries', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(policyResponse())
    .mockResolvedValueOnce(response({ result: { items: [campaign], cursor: null } }))
    .mockResolvedValueOnce(policyResponse())
  vi.stubGlobal('fetch', fetch)
  expect(await listManagedCampaigns(policy, binding, null, signal())).toEqual({ items: [campaign], cursor: null })
  expect(fetch).toHaveBeenNthCalledWith(2, '/api/v1/ads/managed/tools/list_campaigns', expect.objectContaining({
    method: 'POST', redirect: 'error', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic-local-owner' },
    body: JSON.stringify({ grant_id: binding.grant_id, arguments: { limit: 50, cursor: null }, expected_binding: binding }),
  }))
})
it.each([401, 403, 500])('never refreshes/replays or exposes error bodies (%s)', async status => {
  const fetch = vi.fn().mockResolvedValueOnce(policyResponse()).mockResolvedValueOnce(response({ secret: 'must-not-echo' }, status))
  vi.stubGlobal('fetch', fetch)
  await expect(proposeManagedChange(policy, binding, campaign, 'budget', '10.00', 'Revisión humana', signal())).rejects.toThrow('unavailable')
  expect(fetch).toHaveBeenCalledTimes(2)
})
it('preserves decimal string exactly for a proposal, without approving', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(policyResponse()).mockResolvedValueOnce(response({ result: proposal })).mockResolvedValueOnce(policyResponse())
  vi.stubGlobal('fetch', fetch)
  expect(await proposeManagedChange(policy, binding, campaign, 'budget', '10.00', 'Revisión', signal())).toEqual(proposal)
  expect(JSON.parse(fetch.mock.calls[1][1].body).arguments.new_daily_budget_amount).toBe('10.00')
  expect(fetch.mock.calls.every(([url]) => !url.includes('/approve'))).toBe(true)
})
it.each(['-1', '10,00', '1e2', 'NaN', '1.001'])('invalid amount %s is rejected before request', async amount => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  await expect(proposeManagedChange(policy, binding, campaign, 'budget', amount, 'Revisión', signal())).rejects.toThrow('invalid_response')
  expect(fetch).not.toHaveBeenCalled()
})
it.each(['before', 'after'])('changed binding revision %s call denies disclosure without fallback', async when => {
  const changed = response({ policy: { ...policy, bindings: [{ ...binding, revision: 2 }] } })
  const fetch = when === 'before' ? vi.fn().mockResolvedValueOnce(changed)
    : vi.fn().mockResolvedValueOnce(policyResponse()).mockResolvedValueOnce(response({ result: { items: [campaign], cursor: null } })).mockResolvedValueOnce(changed)
  vi.stubGlobal('fetch', fetch)
  await expect(listManagedCampaigns(policy, binding, null, signal())).rejects.toThrow('context_changed')
  expect(fetch).toHaveBeenCalledTimes(when === 'before' ? 1 : 3)
  expect(fetch.mock.calls.every(([url]) => !url.includes('bridge'))).toBe(true)
})
it('removal of the exact assignment does not select another account', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ policy: { ...policy, bindings: [{ ...binding, grant_id: 'other' }] } })))
  await expect(listManagedCampaigns(policy, binding, null, signal())).rejects.toThrow('context_changed')
})
it.each([undefined, { mode: 'free', bindings: [] }, { ...policy, bindings: [{ ...binding, instance_id: 'other' }] },
  { ...policy, central_origin: 'http://ads.example' }, { ...policy, central_origin: 'https://user:secret@ads.example' },
  { ...policy, bindings: [{ ...binding, capabilities: ['approve'] }] }, { ...policy, bindings: [{ ...binding, revision: true }] },
])('invalid policy is not interpreted as free', async value => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ policy: value })))
  await expect(getAdsPolicy(signal())).rejects.toThrow('invalid_response')
})
it('valid review link contains only the non-bearer intent and no assertion', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(policyResponse()).mockResolvedValueOnce(response({ result: review })).mockResolvedValueOnce(policyResponse())
  vi.stubGlobal('fetch', fetch)
  expect(await prepareManagedReview(policy, binding, proposal.proposal_id, signal())).toEqual(review)
})
it.each(['javascript:alert(1)', 'https://enterprise.example/?token=x#/ads/approve/x', 'https://enterprise.example/#/ads/approve/other'])('rejects invalid review URL %s', async url => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(policyResponse()).mockResolvedValueOnce(response({ result: { ...review, review_url: url } })).mockResolvedValueOnce(policyResponse()))
  await expect(prepareManagedReview(policy, binding, proposal.proposal_id, signal())).rejects.toThrow('invalid_response')
})
it('metrics for a different entity are never displayed', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(policyResponse()).mockResolvedValueOnce(response({ result: { entity_ref: 'other', granularity: 'daily', points: [] } })).mockResolvedValueOnce(policyResponse()))
  await expect(getManagedMetrics(policy, binding, campaign.entity_ref, signal())).rejects.toThrow('invalid_response')
})
it('no active local owner session means no network request', async () => {
  auth.mockReturnValue({ kind: 'unauthenticated' })
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  await expect(getAdsPolicy(signal())).rejects.toThrow('unavailable')
  expect(fetch).not.toHaveBeenCalled()
})
