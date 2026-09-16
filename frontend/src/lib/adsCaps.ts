import { token } from './token'

export type Business = { business_id: string; name: string }
export type Guardrail = { daily_cap: number; monthly_cap: number; budget_floor: number; budget_ceiling: number; max_step_pct: number; max_changes_per_entity_per_day: number; currency: string }
export type CapsAccount = { account_ref: string; platform: 'google' | 'meta'; platform_account_id: string; display_name: string | null; currency: string; guardrail: Guardrail | null }
export type Limits = { daily_cap_minor: number; monthly_cap_minor: number; floor_minor: number; ceiling_minor: number; max_step_pct: number; max_changes_per_day: number }
export type Snapshot = { revision: string; loaded_digest: string | null; accounts: Record<string, Limits & { autonomy_enabled: boolean }> }
type Invoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>
const HASH = /^[a-f0-9]{64}$/
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i
export const TWO_DECIMAL_CURRENCIES = new Set('EUR USD GBP CHF CAD AUD NZD MXN BRL ARS COP PEN UYU CNY HKD SGD INR ZAR SEK NOK DKK PLN CZK HUF RON TRY ILS AED SAR THB PHP IDR MYR'.split(' '))
function invoke(): Invoke | undefined { return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke }
export function supportsNativeCaps(): boolean { return typeof invoke() === 'function' }
function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value) }
const SAFE = new Set(['ads_caps_unavailable', 'ads_caps_invalid', 'ads_caps_changed', 'ads_caps_busy', 'ads_caps_restored', 'ads_caps_rollback_unknown', 'ads_caps_session', 'ads_caps_forbidden', 'ads_caps_sql_pending'])
export function capsError(error: unknown): string {
  const key = typeof error === 'string' ? error : error instanceof Error ? error.message : ''
  return SAFE.has(key) ? key : 'ads_caps_unavailable'
}
async function native(command: string, args?: Record<string, unknown>): Promise<unknown> {
  try { const call = invoke(); if (!call) throw new Error('ads_caps_unavailable'); return await call(command, args) }
  catch (error) { throw new Error(capsError(error)) }
}
async function request(path: string, init?: RequestInit): Promise<unknown> {
  const key = token()
  const res = await fetch(`/ads/api/v1${path}`, { ...init, credentials: 'include', headers: { ...(key ? { Authorization: `Bearer ${key}` } : {}), ...init?.headers }, signal: AbortSignal.timeout(20_000) })
  if (res.status === 401) throw new Error('ads_caps_session')
  if (res.status === 403) throw new Error('ads_caps_forbidden')
  if (!res.ok) throw new Error('ads_caps_unavailable')
  return res.json()
}
export async function listCapsBusinesses(): Promise<Business[]> {
  const result = await request('/auth/me')
  if (!record(result) || !Array.isArray(result.businesses) || result.businesses.some(item => !record(item) || typeof item.business_id !== 'string' || !UUID.test(item.business_id) || typeof item.name !== 'string')) throw new Error('ads_caps_unavailable')
  return result.businesses as Business[]
}
function isGuardrail(value: unknown): value is Guardrail {
  return record(value) && ['daily_cap', 'monthly_cap', 'budget_floor', 'budget_ceiling', 'max_step_pct', 'max_changes_per_entity_per_day'].every(key => typeof value[key] === 'number' && Number.isFinite(value[key]) && value[key] >= 0) && typeof value.currency === 'string'
}
export async function listCapsAccounts(business: string): Promise<CapsAccount[]> {
  if (!UUID.test(business)) throw new Error('ads_caps_invalid')
  const result = await request(`/guardrails/setup?business_id=${encodeURIComponent(business)}`)
  if (!record(result) || !Array.isArray(result.items)) throw new Error('ads_caps_unavailable')
  for (const item of result.items) {
    if (!record(item) || !['google', 'meta'].includes(String(item.platform)) || typeof item.account_ref !== 'string' || !/^(google|meta):[A-Za-z0-9_]+$/.test(item.account_ref) || typeof item.platform_account_id !== 'string' || !(item.platform === 'google' ? /^[0-9]{10}$/.test(item.platform_account_id) : /^act_[0-9]{5,30}$/.test(item.platform_account_id)) || typeof item.currency !== 'string' || !/^[A-Z]{3}$/.test(item.currency) || !(item.display_name === null || typeof item.display_name === 'string') || !(item.guardrail === null || isGuardrail(item.guardrail))) throw new Error('ads_caps_unavailable')
  }
  return result.items as CapsAccount[]
}
export function parseMinor(value: string): number | null {
  if (!/^\d{1,10}(?:[.,]\d{1,2})?$/.test(value.trim())) return null
  const [major, fraction = ''] = value.trim().replace(',', '.').split('.')
  const minor = Number(major) * 100 + Number(fraction.padEnd(2, '0'))
  return Number.isSafeInteger(minor) && minor <= 1_000_000_000_000 ? minor : null
}
function isLimits(value: unknown): value is Limits {
  return record(value) && ['daily_cap_minor', 'monthly_cap_minor', 'floor_minor', 'ceiling_minor', 'max_changes_per_day'].every(key => typeof value[key] === 'number' && Number.isSafeInteger(value[key]) && value[key] >= 0) && typeof value.max_step_pct === 'number' && Number.isFinite(value.max_step_pct)
}
export async function getNativeCaps(): Promise<Snapshot> {
  const value = await native('get_ads_hard_caps')
  if (!record(value) || typeof value.revision !== 'string' || !HASH.test(value.revision) || !(value.loaded_digest === null || typeof value.loaded_digest === 'string' && HASH.test(value.loaded_digest)) || !record(value.accounts) || Object.values(value.accounts).some(cap => !record(cap) || typeof cap.autonomy_enabled !== 'boolean' || !isLimits(cap))) throw new Error('ads_caps_unavailable')
  return value as Snapshot
}
export function sameGuardrail(guardrail: Guardrail | null, limits: Limits, currency: string): boolean {
  return !!guardrail && guardrail.currency === currency && parseMinor(String(guardrail.daily_cap)) === limits.daily_cap_minor && parseMinor(String(guardrail.monthly_cap)) === limits.monthly_cap_minor && parseMinor(String(guardrail.budget_floor)) === limits.floor_minor && parseMinor(String(guardrail.budget_ceiling)) === limits.ceiling_minor && guardrail.max_step_pct === limits.max_step_pct && guardrail.max_changes_per_entity_per_day === limits.max_changes_per_day
}
export function readAdsCsrf(frame: HTMLIFrameElement | null): string | null {
  try {
    const location = frame?.contentWindow?.location
    if (!location || location.origin !== window.location.origin || !location.pathname.startsWith('/ads/')) return null
    const match = frame?.contentDocument?.cookie.match(/(?:^|;\s*)ads_csrf=([^;]+)/)
    return match ? decodeURIComponent(match[1]) : null
  } catch { return null }
}
export async function saveCaps(business: string, account: CapsAccount, snapshot: Snapshot, limits: Limits, csrf: string | null): Promise<Snapshot | null> {
  if (!csrf) throw new Error('ads_caps_session')
  if (!UUID.test(business) || !TWO_DECIMAL_CURRENCIES.has(account.currency)) throw new Error('ads_caps_invalid')
  const saved = await native('save_ads_hard_caps', { change: { revision: snapshot.revision, platform: account.platform, account_id: account.platform_account_id, currency: account.currency, ...limits } })
  if (saved === null) return null
  if (!record(saved) || typeof saved.revision !== 'string' || !HASH.test(saved.revision) || saved.loaded_digest !== saved.revision) throw new Error('ads_caps_unavailable')
  try {
    await request(`/guardrails/${encodeURIComponent(account.account_ref)}?business_id=${encodeURIComponent(business)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ daily_cap: limits.daily_cap_minor / 100, monthly_cap: limits.monthly_cap_minor / 100, budget_floor: limits.floor_minor / 100, budget_ceiling: limits.ceiling_minor / 100, max_step_pct: limits.max_step_pct, max_changes_per_entity_per_day: limits.max_changes_per_day }) })
    const [accounts, verified] = await Promise.all([listCapsAccounts(business), getNativeCaps()])
    const current = accounts.find(item => item.account_ref === account.account_ref)
    if (!current || current.platform_account_id !== account.platform_account_id || !sameGuardrail(current.guardrail, limits, account.currency) || verified.revision !== saved.revision || verified.loaded_digest !== saved.revision) throw new Error('ads_caps_sql_pending')
    return verified
  } catch { throw new Error('ads_caps_sql_pending') }
}
