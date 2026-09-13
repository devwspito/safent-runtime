import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); vi.resetModules() })
it.each([{ ok: false, error: 'private failure' }, {}, null, { provider_id: 'one', is_active: false }])('does not claim provider activation from an error or malformed 200 response %#', async value => {
  localStorage.setItem('safent_token', 'owner-test'); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(value)))
  const { setActiveProvider } = await import('./client')
  await expect(setActiveProvider('one')).rejects.toMatchObject({ status: 502, body: null })
})
it.each([{ provider_id: 'one', is_active: true }, { provider_id: 'one', ok: true }])('accepts the existing SQL and native successful activation shapes %#', async value => {
  localStorage.setItem('safent_token', 'owner-test'); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(value)))
  const { setActiveProvider } = await import('./client')
  await expect(setActiveProvider('one')).resolves.toEqual(value)
})
it('reads only real catalog IDs and strips unrelated daemon fields', async () => {
  localStorage.setItem('safent_token', 'owner-test')
  const fetch = vi.fn().mockResolvedValue(Response.json({ provider_id: 'openai-codex', active_model: 'one', models: ['one', 'two', 'two'], token: 'not-returned' }))
  vi.stubGlobal('fetch', fetch)
  const { getNativeModelCatalog } = await import('./client')
  await expect(getNativeModelCatalog()).resolves.toEqual({ provider_id: 'openai-codex', active_model: 'one', models: ['one', 'two'] })
  expect(fetch).toHaveBeenCalledWith('/api/v1/providers/native/models', expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer owner-test' }) }))
})
it('changes only the model with a compare-and-set precondition, never OAuth secrets', async () => {
  localStorage.setItem('safent_token', 'owner-test')
  const fetch = vi.fn().mockResolvedValue(Response.json({ provider_id: 'openai-codex', active_model: 'two' }))
  vi.stubGlobal('fetch', fetch)
  const { selectNativeModel } = await import('./client')
  await selectNativeModel({ provider_id: 'openai-codex', model: 'two', active_model: 'one' })
  expect(fetch).toHaveBeenCalledWith('/api/v1/providers/native/model', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ provider_id: 'openai-codex', model: 'two', expected_model: 'one' }) }))
})
it.each([null, {}, { provider_id: 'custom', active_model: 'one', models: ['one'] }, { provider_id: 'openai-codex', active_model: 'one', models: [] }, { provider_id: 'openai-codex', active_model: 'one', models: ['bad/model'] }])('rejects malformed/unavailable catalogs without offering fallback %#', async value => {
  localStorage.setItem('safent_token', 'owner-test'); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(value)))
  const { getNativeModelCatalog } = await import('./client')
  await expect(getNativeModelCatalog()).rejects.toMatchObject({ status: 502 })
})
it('does not report success for a different returned model', async () => {
  localStorage.setItem('safent_token', 'owner-test'); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ provider_id: 'openai-codex', active_model: 'other' })))
  const { selectNativeModel } = await import('./client')
  await expect(selectNativeModel({ provider_id: 'openai-codex', model: 'two', active_model: 'one' })).rejects.toMatchObject({ status: 502 })
})
