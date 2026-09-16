import { afterEach, beforeEach, expect, it, vi } from 'vitest'

beforeEach(() => { localStorage.setItem('safent_token', 'fictitious-test-session') })
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })

function respond(payload: unknown, status = 200) {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

const account = { id: 'ca_actual_123', toolkit_slug: 'gmail', entity_id: 'owner_scope', status: 'ACTIVE', auth_config_id: 'ac_123' }

it('reads the real REST connected-account shape and preserves identity and authorization metadata', async () => {
  const fetch = respond([account])
  const result = await (await import('./client')).listComposioConnected()
  expect(result).toEqual([account])
  expect(result[0]).not.toHaveProperty('slug')
  expect(fetch).toHaveBeenCalledWith('/api/v1/integrations/composio/connected', expect.anything())
})

it('normalizes toolkit and status casing while retaining both accounts on one platform', async () => {
  respond([{ ...account, toolkit_slug: ' GMAIL ', status: 'active', auth_config_id: undefined }, { ...account, id: 'ca_second' }])
  expect(await (await import('./client')).listComposioConnected()).toEqual([
    { ...account, auth_config_id: '' }, { ...account, id: 'ca_second' },
  ])
})

it('keeps a confirmed empty connected list distinct from failure', async () => {
  respond([])
  await expect((await import('./client')).listComposioConnected()).resolves.toEqual([])
})

it.each([
  null, {}, { items: [account] }, [null], [{ slug: 'gmail', name: 'Gmail' }],
  [{ ...account, id: '' }], [{ ...account, toolkit_slug: ' ' }],
  [{ ...account, entity_id: null }], [{ ...account, status: 1 }],
  [{ ...account, auth_config_id: false }], [account, account],
])('rejects malformed connected data (%j) with a human retry error', async payload => {
  respond(payload)
  await expect((await import('./client')).listComposioConnected()).rejects.toMatchObject({
    status: 502, message: 'No se pudieron verificar tus conexiones. Vuelve a intentarlo.',
  })
})

it('preserves a server lookup failure instead of reporting no connections', async () => {
  respond({ detail: 'Servicio temporalmente no disponible' }, 503)
  await expect((await import('./client')).listComposioConnected()).rejects.toMatchObject({ status: 503 })
})

it('normalizes optional catalog names without mixing them with accounts', async () => {
  respond([{ slug: ' GoogleAds ', name: ' Google Ads ', description: ' Ads ' }, { slug: 'gmail', name: '' }, { slug: 'custom_tool' }])
  expect(await (await import('./client')).listComposioApps()).toEqual([
    { slug: 'googleads', name: 'Google Ads', description: 'Ads', logo: undefined },
    { slug: 'gmail', name: undefined, description: undefined, logo: undefined },
    { slug: 'custom_tool', name: undefined, description: undefined, logo: undefined },
  ])
})

it.each([null, {}, [account], [{ slug: ' ' }], [{ slug: 'gmail', name: 42 }]])('rejects malformed catalogs (%j)', async payload => {
  respond(payload)
  await expect((await import('./client')).listComposioApps()).rejects.toMatchObject({ status: 502 })
})

it('revokes only the supplied real connection ID, safely encoded', async () => {
  const fetch = respond({ status: 'deleted' })
  await (await import('./client')).disconnectComposioApp('ca_actual/123?x')
  expect(fetch).toHaveBeenCalledWith('/api/v1/integrations/composio/connected/ca_actual%2F123%3Fx', expect.objectContaining({ method: 'DELETE' }))
})

const metaCredentials = { client_id: '1234567890', client_secret: 'synthetic-meta-secret-only' }

it.each(['googleads', 'metaads'] as const)('reads saved %s configuration as readiness without retaining IDs or extra fields', async toolkit => {
  const fetch = respond({ toolkit_slug: toolkit, auth_config_id: 'ac_test', secret: 'do-not-retain' })
  expect(await (await import('./client')).getComposioAdsConfig(toolkit)).toEqual({ ready: true })
  expect(fetch).toHaveBeenCalledWith(`/api/v1/integrations/composio/auth-configs/${toolkit}`, expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer fictitious-test-session' }) }))
})

it('only an explicit null config ID means preparation is absent', async () => {
  respond({ toolkit_slug: 'metaads', auth_config_id: null })
  expect(await (await import('./client')).getComposioAdsConfig('metaads')).toEqual({ ready: false })
})

it.each([null, {}, [], { toolkit_slug: 'googleads', auth_config_id: 'ac_test' }, { toolkit_slug: 'metaads' }, { toolkit_slug: 'metaads', auth_config_id: '' }, { toolkit_slug: 'metaads', auth_config_id: false }])('rejects malformed saved configuration %# instead of claiming absence', async payload => {
  respond(payload)
  await expect((await import('./client')).getComposioAdsConfig('metaads')).rejects.toMatchObject({ status: 502, body: null })
})

it('refuses other toolkit paths before calling fetch', async () => {
  const fetch = respond({})
  await expect((await import('./client')).getComposioAdsConfig('../other' as 'metaads')).rejects.toMatchObject({ status: 400, body: null })
  expect(fetch).not.toHaveBeenCalled()
})

it('prepares Ads with no request body and returns only strict readiness booleans', async () => {
  const fetch = respond({ googleads: true, metaads: false, secret: 'do-not-retain' })
  expect(await (await import('./client')).prepareComposioAds()).toEqual({ googleads: true, metaads: false })
  expect(fetch).toHaveBeenCalledWith('/api/v1/integrations/composio/ads/prepare', expect.objectContaining({ method: 'POST' }))
  expect(fetch.mock.calls[0][1]).not.toHaveProperty('body')
})

it.each(['meta', 'google'] as const)('allows the backend preparation and companion refresh time for %s', async provider => {
  const timeout = vi.spyOn(globalThis, 'setTimeout')
  respond(provider === 'meta' ? { ready: true } : { googleads: true, metaads: false })
  const client = await import('./client')
  if (provider === 'meta') await client.setupComposioMeta(metaCredentials)
  else await client.prepareComposioAds()
  expect(timeout).toHaveBeenCalledWith(expect.any(Function), 60_000)
  timeout.mockRestore()
})

it.each([null, {}, { googleads: true }, { googleads: 'true', metaads: false }, { googleads: false, metaads: null }])('rejects malformed preparation readiness %#', async payload => {
  respond(payload)
  await expect((await import('./client')).prepareComposioAds()).rejects.toMatchObject({ status: 502, body: null })
})

it('sends the Composio key explicitly and retains only confirmed readiness', async () => {
  const fetch = respond({ has_key: true, api_key: 'synthetic-key-only' })
  expect(await (await import('./client')).setComposioApiKey('synthetic-key-only')).toEqual({ has_key: true })
  expect(fetch).toHaveBeenCalledWith('/api/v1/integrations/composio/key', expect.objectContaining({ method: 'POST', body: JSON.stringify({ api_key: 'synthetic-key-only' }) }))
  expect(localStorage.length).toBe(1)
})

it.each([null, {}, { has_key: false }, { has_key: 'true' }])('rejects unconfirmed key readiness %#', async payload => {
  respond(payload)
  await expect((await import('./client')).setComposioApiKey('synthetic-key-only')).rejects.toMatchObject({ status: 502, body: null })
})

it.each([403, 409, 429, 503])('redacts all setup wrapper errors from HTTP %s', async status => {
  respond({ detail: 'synthetic-key-only', credentials: 'synthetic-key-only' }, status)
  const client = await import('./client')
  for (const operation of [() => client.setComposioApiKey('synthetic-key-only'), () => client.getComposioAdsConfig('metaads'), () => client.prepareComposioAds()]) {
    const failure = await operation().catch(error => error)
    expect(failure).toMatchObject({ status, body: null })
    expect(failure.message).not.toContain('synthetic-key-only')
    expect(failure.cause).toBeUndefined()
  }
})

it('prepares Meta through the owner endpoint and exposes only readiness', async () => {
  const fetch = respond({ ready: true, client_secret: metaCredentials.client_secret, account: 'not-authorized' })
  expect(await (await import('./client')).setupComposioMeta(metaCredentials)).toEqual({ ready: true })
  expect(fetch).toHaveBeenCalledWith('/api/v1/integrations/composio/meta/setup', expect.objectContaining({
    method: 'POST', body: JSON.stringify(metaCredentials),
    headers: expect.objectContaining({ Authorization: 'Bearer fictitious-test-session' }),
  }))
  expect(localStorage.getItem('safent_token')).toBe('fictitious-test-session')
  expect(localStorage.length).toBe(1)
})

it.each([null, {}, { ready: false }, { ready: 'true' }, { connected: true }, []])('rejects unconfirmed Meta readiness (%j)', async payload => {
  respond(payload)
  await expect((await import('./client')).setupComposioMeta(metaCredentials)).rejects.toMatchObject({ status: 502, body: null })
})

it.each([400, 403, 429, 500, 502, 503])('does not retain credential-bearing Meta errors from HTTP %s', async status => {
  respond({ detail: { message: metaCredentials.client_secret, client_secret: metaCredentials.client_secret } }, status)
  const failure = await (await import('./client')).setupComposioMeta(metaCredentials).catch(error => error)
  expect(failure).toMatchObject({ status, body: null, message: 'No se pudo preparar Meta Ads.' })
  expect(failure.message).not.toContain(metaCredentials.client_secret)
  expect(failure.cause).toBeUndefined()
  expect(JSON.stringify(failure)).not.toContain(metaCredentials.client_secret)
})

it('does not retain credential-bearing network errors from Meta setup', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error(metaCredentials.client_secret)))
  await expect((await import('./client')).setupComposioMeta(metaCredentials)).rejects.toMatchObject({ status: 0, body: null, message: 'No se pudo preparar Meta Ads.' })
})

it('does not send Meta credentials without an authenticated session', async () => {
  localStorage.clear()
  const fetch = respond({ ready: true })
  await expect((await import('./client')).setupComposioMeta(metaCredentials)).rejects.toMatchObject({ status: 401, body: null })
  expect(fetch).not.toHaveBeenCalled()
})
