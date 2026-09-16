import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getNativeCaps, listCapsAccounts, listCapsBusinesses, parseMinor, readAdsCsrf, saveCaps, type CapsAccount, type Limits, type Snapshot } from './adsCaps'
vi.mock('./token', () => ({ token: () => 'owner-session' }))
const business = '11111111-1111-4111-8111-111111111111'
const before = 'a'.repeat(64), after = 'b'.repeat(64)
const account: CapsAccount = { account_ref: 'google:1234567890', platform: 'google', platform_account_id: '1234567890', display_name: null, currency: 'EUR', guardrail: null }
const limits: Limits = { daily_cap_minor: 1000, monthly_cap_minor: 10000, floor_minor: 0, ceiling_minor: 1000, max_step_pct: 15, max_changes_per_day: 2 }
const old: Snapshot = { revision: before, loaded_digest: before, accounts: {} }
const updated: Snapshot = { revision: after, loaded_digest: after, accounts: { [account.platform_account_id]: { ...limits, autonomy_enabled: false } } }
const guardrail = { daily_cap: 10, monthly_cap: 100, budget_floor: 0, budget_ceiling: 10, max_step_pct: 15, max_changes_per_entity_per_day: 2, currency: 'EUR' }
let invoke: ReturnType<typeof vi.fn>, fetchMock: ReturnType<typeof vi.fn>
function response(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }) }
beforeEach(() => { invoke = vi.fn().mockResolvedValue(old); fetchMock = vi.fn(); vi.stubGlobal('fetch', fetchMock); Object.defineProperty(window, '__TAURI__', { configurable: true, value: { core: { invoke } } }) })
afterEach(() => { vi.unstubAllGlobals(); Reflect.deleteProperty(window, '__TAURI__') })
it.each([['10', 1000], ['0', 0], ['10,23', 1023], ['10.1', 1010], ['-1', null], ['1e2', null], ['1.234', null], ['', null], ['Infinity', null]])('parses exact minor units %s', (value, expected) => { expect(parseMinor(String(value))).toBe(expected) })
it('uses real owner business/account/currency and preserves missing guardrail', async () => { fetchMock.mockResolvedValueOnce(response({ businesses: [{ business_id: business, name: 'Clinic' }] })).mockResolvedValueOnce(response({ items: [account] })); expect(await listCapsBusinesses()).toEqual([{ business_id: business, name: 'Clinic' }]); expect(await listCapsAccounts(business)).toEqual([account]); expect(fetchMock.mock.calls[1][0]).toContain(`business_id=${business}`) })
it.each([401, 403, 500])('keeps API failure %s redacted and performs no native mutation', async status => { fetchMock.mockResolvedValue(response({ private: 'secret' }, status)); await expect(listCapsAccounts(business)).rejects.toThrow(/^ads_caps_/); expect(invoke).not.toHaveBeenCalled() })
it('rejects malformed native snapshot and unsupported payload', async () => { invoke.mockResolvedValueOnce({ ...old, revision: '../path' }); await expect(getNativeCaps()).rejects.toThrow('ads_caps_unavailable'); fetchMock.mockResolvedValueOnce(response({ items: [{ ...account, platform_account_id: '../other' }] })); await expect(listCapsAccounts(business)).rejects.toThrow('ads_caps_unavailable') })
it('native cancellation never sends PUT or changes SQL', async () => { invoke.mockResolvedValue(null); expect(await saveCaps(business, account, old, limits, 'csrf')).toBeNull(); expect(fetchMock).not.toHaveBeenCalled(); expect(invoke).toHaveBeenCalledExactlyOnceWith('save_ads_hard_caps', { change: { revision: before, platform: 'google', account_id: '1234567890', currency: 'EUR', ...limits } }); expect(JSON.stringify(invoke.mock.calls)).not.toContain('autonomy_enabled') })
it('requires CSRF before invoking a native mutation', async () => { await expect(saveCaps(business, account, old, limits, null)).rejects.toThrow('ads_caps_session'); expect(invoke).not.toHaveBeenCalled() })
it('verifies loaded ACK and SQL readback after applying exact owner amounts', async () => {
  invoke.mockResolvedValueOnce({ revision: after, loaded_digest: after }).mockResolvedValueOnce(updated)
  fetchMock.mockResolvedValueOnce(response({ account_ref: account.account_ref })).mockResolvedValueOnce(response({ items: [{ ...account, guardrail }] }))
  expect(await saveCaps(business, account, old, limits, 'csrf')).toEqual(updated)
  const [url, request] = fetchMock.mock.calls[0]
  expect(url).toContain('/guardrails/google%3A1234567890?business_id=')
  expect(request.headers['X-CSRF-Token']).toBe('csrf')
  expect(JSON.parse(request.body).daily_cap).toBe(10)
})
it('does not accept wrong native digest or stale SQL readback as success', async () => {
  invoke.mockResolvedValueOnce({ revision: after, loaded_digest: before })
  await expect(saveCaps(business, account, old, limits, 'csrf')).rejects.toThrow('ads_caps_unavailable'); expect(fetchMock).not.toHaveBeenCalled()
  invoke.mockResolvedValueOnce({ revision: after, loaded_digest: after }).mockResolvedValueOnce(updated)
  fetchMock.mockResolvedValueOnce(response({})).mockResolvedValueOnce(response({ items: [account] }))
  await expect(saveCaps(business, account, old, limits, 'csrf')).rejects.toThrow('ads_caps_sql_pending')
})
it('reads CSRF only from the same-origin Ads frame', () => {
  expect(readAdsCsrf(null)).toBeNull()
  const frame = { contentWindow: { location: { origin: window.location.origin, pathname: '/ads/reglas' } }, contentDocument: { cookie: 'ads_csrf=token123' } } as unknown as HTMLIFrameElement
  expect(readAdsCsrf(frame)).toBe('token123')
  expect(readAdsCsrf({ ...frame, contentWindow: { location: { origin: 'https://evil.test', pathname: '/ads/' } } } as unknown as HTMLIFrameElement)).toBeNull()
})
