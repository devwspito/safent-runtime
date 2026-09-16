import { getAuthStatus, token } from '../lib/token'

export interface AdsBinding {
  grant_id: string; revision: number; org_id: string; user_id: string
  employee_id: string; instance_id: string; business_id: string
  platform: 'google' | 'meta'; connection_id: string; external_account_id: string
  resource_revision: number; role: 'ads'; capabilities: ['read', 'propose', 'approve', 'execute']
}
export interface AdsPolicy {
  mode: 'free' | 'managed'; instance_id: string; central_origin: string | null; bindings: AdsBinding[]
}
export interface AdsCampaign {
  entity_ref: string; name: string; status: 'active' | 'paused' | 'removed'
  budget: { amount: number; currency: string }; is_controllable: boolean
}
export interface AdsCampaignPage { items: AdsCampaign[]; cursor: string | null }
export interface AdsMetrics {
  entity_ref: string; granularity: 'daily'
  points: { period_start: string; spend: { amount: number; currency: string }; conversions: number }[]
}
export interface AdsProposal { proposal_id: string; estado: string; diff_hash: string; expires_at: string }
export interface AdsReview { proposal_id: string; intent_id: string; expires_at: string; review_url: string }

export class ManagedAdsError extends Error {
  constructor(readonly kind: 'unavailable' | 'context_changed' | 'invalid_response' = 'unavailable') {
    super(kind) // No upstream body, URL, token or user-supplied text in errors.
  }
}
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/
const text = (value: unknown, max = 128): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= max && !/[\x00-\x1f\x7f]/.test(value)
const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value)
const version = (value: unknown): value is number => Number.isInteger(value) && Number(value) > 0 && Number(value) <= 2147483647
const date = (value: unknown): value is string => text(value) && Number.isFinite(Date.parse(value))
function invalid(): never { throw new ManagedAdsError('invalid_response') }

function binding(value: unknown): AdsBinding {
  if (!record(value) || !['grant_id', 'org_id', 'user_id', 'employee_id', 'instance_id'].every(key => text(value[key]))
    || !version(value.revision) || !version(value.resource_revision)
    || typeof value.business_id !== 'string' || !UUID.test(value.business_id)
    || typeof value.connection_id !== 'string' || !UUID.test(value.connection_id)
    || !text(value.external_account_id) || !/^\d+$/.test(value.external_account_id)
    || (value.platform !== 'google' && value.platform !== 'meta') || value.role !== 'ads'
    || JSON.stringify(value.capabilities) !== '["read","propose","approve","execute"]') invalid()
  // Reconstruct only the public contract. Unknown properties never reach UI/state.
  return Object.fromEntries([
    'grant_id', 'revision', 'org_id', 'user_id', 'employee_id', 'instance_id', 'business_id',
    'platform', 'connection_id', 'external_account_id', 'resource_revision', 'role', 'capabilities',
  ].map(key => [key, value[key]])) as unknown as AdsBinding
}
export function adsBindingKey(value: AdsBinding): string {
  return JSON.stringify(binding(value))
}
export function adsPolicyKey(value: AdsPolicy): string {
  return JSON.stringify([value.mode, value.instance_id, value.central_origin, value.bindings.map(adsBindingKey)])
}
function httpsUrl(value: unknown): URL {
  if (!text(value, 1000) || !/^[\x21-\x7e]+$/.test(value) || value.includes('\\')) invalid()
  let url: URL
  try { url = new URL(value) } catch { return invalid() }
  if (url.protocol !== 'https:' || url.username || url.password || url.port && url.port !== '443' || url.search) invalid()
  return url
}

// Separate from client.request: it retries a 401 after refreshing credentials.
// Here even proposal/review POSTs are one-shot. Neither endpoint calls providers
// directly; the backend remains the authority for each admission.
async function request(path: string, signal: AbortSignal, payload?: unknown): Promise<unknown> {
  if (getAuthStatus().kind !== 'authenticated') throw new ManagedAdsError()
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) abort()
  const timer = setTimeout(abort, 20_000)
  try {
    const response = await fetch(`/api/v1/ads/managed${path}`, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token()}` },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal: controller.signal, cache: 'no-store', redirect: 'error',
    })
    if (!response.ok) throw new ManagedAdsError()
    const raw = await response.text()
    if (controller.signal.aborted) throw new ManagedAdsError()
    if (raw.length > 524288) invalid()
    try { return JSON.parse(raw) as unknown } catch { return invalid() }
  } catch (error) {
    if (error instanceof ManagedAdsError) throw error
    throw new ManagedAdsError()
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', abort)
  }
}

export async function getAdsPolicy(signal: AbortSignal): Promise<AdsPolicy | null> {
  const result = await request('', signal)
  if (!record(result)) invalid()
  if (result.policy === null) return null // Server permits this only when never associated.
  const p = result.policy
  if (!record(p) || !text(p.instance_id) || !Array.isArray(p.bindings) || p.bindings.length > 100) invalid()
  const bindings = p.bindings.map(binding)
  if (bindings.some(item => item.instance_id !== p.instance_id) || new Set(bindings.map(item => item.grant_id)).size !== bindings.length) invalid()
  if (p.mode === 'free' && p.central_origin === null && bindings.length === 0)
    return { mode: 'free', instance_id: p.instance_id, central_origin: null, bindings }
  if (p.mode !== 'managed') invalid()
  const origin = httpsUrl(p.central_origin)
  if (origin.hash || !text(p.central_origin, 512) || p.central_origin.endsWith('/') || origin.pathname !== '/') invalid()
  return { mode: 'managed', instance_id: p.instance_id, central_origin: p.central_origin, bindings }
}

async function call(policy: AdsPolicy, selected: AdsBinding, name: string, args: object, signal: AbortSignal): Promise<Record<string, unknown>> {
  const expected = adsPolicyKey(policy)
  const verify = async () => {
    const current = await getAdsPolicy(signal)
    if (!current || current.mode !== 'managed' || adsPolicyKey(current) !== expected
      || !current.bindings.some(item => adsBindingKey(item) === adsBindingKey(selected)))
      throw new ManagedAdsError('context_changed')
  }
  await verify()
  const result = await request(`/tools/${name}`, signal, {
    grant_id: selected.grant_id, arguments: args, expected_binding: selected,
  })
  // Never disclose a late result under another account/policy or a revoked scope.
  await verify()
  if (!record(result) || !record(result.result)) invalid()
  return result.result
}

export async function listManagedCampaigns(policy: AdsPolicy, selected: AdsBinding, cursor: string | null, signal: AbortSignal): Promise<AdsCampaignPage> {
  const r = await call(policy, selected, 'list_campaigns', { limit: 50, cursor }, signal)
  if (!Array.isArray(r.items) || r.items.length > 50 || r.cursor !== null && !text(r.cursor, 1200)) invalid()
  const items = r.items.map(item => {
    if (!record(item) || !text(item.entity_ref, 1200) || !text(item.name, 1000)
      || typeof item.status !== 'string' || !['active', 'paused', 'removed'].includes(item.status)
      || typeof item.is_controllable !== 'boolean' || !record(item.budget)
      || typeof item.budget.amount !== 'number' || !Number.isFinite(item.budget.amount)
      || typeof item.budget.currency !== 'string' || !/^[A-Z]{3}$/.test(item.budget.currency)) invalid()
    return { entity_ref: item.entity_ref, name: item.name, status: item.status,
      budget: { amount: item.budget.amount, currency: item.budget.currency }, is_controllable: item.is_controllable } as AdsCampaign
  })
  if (new Set(items.map(item => item.entity_ref)).size !== items.length) invalid()
  return { items, cursor: r.cursor as string | null }
}

export async function getManagedMetrics(policy: AdsPolicy, selected: AdsBinding, entity: string, signal: AbortSignal): Promise<AdsMetrics> {
  const r = await call(policy, selected, 'get_entity_metrics', { entity_ref: entity, window: { preset: '7D', lag_days: 0 }, granularity: 'daily' }, signal)
  if (r.entity_ref !== entity || r.granularity !== 'daily' || !Array.isArray(r.points) || r.points.length > 1000) invalid()
  const points = r.points.map(item => {
    if (!record(item) || !date(item.period_start) || !record(item.spend)
      || typeof item.spend.amount !== 'number' || !Number.isFinite(item.spend.amount)
      || typeof item.spend.currency !== 'string' || !/^[A-Z]{3}$/.test(item.spend.currency)
      || !Number.isSafeInteger(item.conversions) || Number(item.conversions) < 0) invalid()
    return { period_start: item.period_start, spend: { amount: item.spend.amount, currency: item.spend.currency }, conversions: item.conversions as number }
  })
  return { entity_ref: entity, granularity: 'daily', points }
}

export async function proposeManagedChange(policy: AdsPolicy, selected: AdsBinding, campaign: AdsCampaign, change: 'pause' | 'budget', amount: string, cause: string, signal: AbortSignal): Promise<AdsProposal> {
  if (!campaign.is_controllable || !cause.trim() || cause.length > 140 || change === 'budget' && !/^\d+(\.\d{1,2})?$/.test(amount)) invalid()
  const r = await call(policy, selected, change === 'pause' ? 'propose_pause' : 'propose_budget_change', {
    business_id: selected.business_id, entity_ref: campaign.entity_ref, cause: { text: cause.trim() },
    ...(change === 'budget' ? { new_daily_budget_amount: amount, new_daily_budget_currency: campaign.budget.currency } : {}),
  }, signal)
  if (!text(r.proposal_id) || !UUID.test(r.proposal_id) || !text(r.estado)
    || typeof r.diff_hash !== 'string' || !/^[a-f0-9]{64}$/.test(r.diff_hash) || !date(r.expires_at)) invalid()
  return { proposal_id: r.proposal_id, estado: r.estado, diff_hash: r.diff_hash, expires_at: r.expires_at }
}

export async function prepareManagedReview(policy: AdsPolicy, selected: AdsBinding, proposalId: string, signal: AbortSignal): Promise<AdsReview> {
  const r = await call(policy, selected, 'get_approval_review', { proposal_id: proposalId }, signal)
  if (r.proposal_id !== proposalId || !text(r.intent_id) || !UUID.test(r.intent_id) || !date(r.expires_at)) invalid()
  const url = httpsUrl(r.review_url)
  if (url.pathname !== '/' || url.hash !== `#/ads/approve/${r.intent_id}`) invalid()
  return { proposal_id: proposalId, intent_id: r.intent_id, expires_at: r.expires_at, review_url: String(r.review_url) }
}
