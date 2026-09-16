import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { CODEX_DEVICE_URL, hasNativeProviderOAuthOpener, openProviderOAuthUrl } from './providerOAuth'

beforeEach(()=>{vi.spyOn(window,'open').mockReturnValue(null)})
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals()})

it('preserves the ordinary browser fallback without claiming a blocked popup succeeded',async()=>{
  expect(hasNativeProviderOAuthOpener()).toBe(false)
  expect(await openProviderOAuthUrl(CODEX_DEVICE_URL)).toBe('unconfirmed')
  expect(window.open).toHaveBeenCalledExactlyOnceWith(CODEX_DEVICE_URL,'_blank','noopener,noreferrer')
})
it('does not fall back to a popup after a native launcher error',async()=>{
  const invoke=vi.fn().mockRejectedValue(new Error('private failure'))
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  expect(hasNativeProviderOAuthOpener()).toBe(true)
  expect(await openProviderOAuthUrl(CODEX_DEVICE_URL)).toBe('failed')
  expect(window.open).not.toHaveBeenCalled()
})
it.each([
  'javascript:alert(1)','file:///tmp/device','https://localhost/codex/device',
  'https://127.0.0.1/codex/device','http://auth.openai.com/codex/device',
  'https://auth.openai.com:443/codex/device','https://user@auth.openai.com/codex/device',
  'https://auth.openai.com/codex/device?code=fixture','https://auth.openai.com/codex/device#fragment',
  'https://auth.openai.com/codex/device/','https://auth.openai.com/oauth/authorize',
  'https://auth.openai.com.evil.example/codex/device','https://connect.composio.dev/link/lk_fixture',
])('native provider opening rejects non-exact URL %s',async url=>{
  const invoke=vi.fn()
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  expect(await openProviderOAuthUrl(url)).toBe('failed')
  expect(invoke).not.toHaveBeenCalled()
  expect(window.open).not.toHaveBeenCalled()
})
it('returns a recoverable failure if the browser throws',async()=>{
  vi.mocked(window.open).mockImplementation(()=>{throw new Error('browser failure')})
  expect(await openProviderOAuthUrl(CODEX_DEVICE_URL)).toBe('failed')
})
