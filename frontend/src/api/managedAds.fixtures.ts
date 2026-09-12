/** Synthetic public metadata for UI tests/visual QA; never provider credentials. */
import type { AdsBinding, AdsPolicy, AdsCampaign } from './managedAds'
export const adsBindingFixture: AdsBinding = {
  grant_id: 'grant-one', revision: 1, org_id: 'org-one', user_id: 'user-one', employee_id: 'employee-one',
  instance_id: 'instance-one', business_id: '11111111-1111-4111-8111-111111111111', platform: 'meta',
  connection_id: '22222222-2222-4222-8222-222222222222', external_account_id: '123456789', resource_revision: 1,
  role: 'ads', capabilities: ['read', 'propose', 'approve', 'execute'],
}
export const adsPolicyFixture: AdsPolicy = { mode: 'managed', instance_id: adsBindingFixture.instance_id,
  central_origin: 'https://ads.example', bindings: [adsBindingFixture] }
export const adsCampaignFixture: AdsCampaign = { entity_ref: 'scoped-campaign-one', name: 'Friendog · Citas de septiembre',
  status: 'active', budget: { amount: 12.5, currency: 'EUR' }, is_controllable: true }
export const adsProposalFixture = { proposal_id: '33333333-3333-4333-8333-333333333333', estado: 'pending_approval',
  diff_hash: 'a'.repeat(64), expires_at: '2099-01-01T12:00:00Z' }
export const adsReviewFixture = { proposal_id: adsProposalFixture.proposal_id, intent_id: '44444444-4444-4444-8444-444444444444',
  expires_at: '2099-01-01T12:00:00Z', review_url: 'https://enterprise.example/#/ads/approve/44444444-4444-4444-8444-444444444444' }
