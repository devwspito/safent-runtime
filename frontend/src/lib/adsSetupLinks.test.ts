import { afterEach, expect, it, vi } from 'vitest'
import { openAdsSetupLink } from './adsSetupLinks'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it.each([
  'https://developers.facebook.com/apps/',
  'https://developers.facebook.com/apps/12345/settings/basic/',
  'https://developers.facebook.com/apps/123456789012345678901234567890/fb-login/settings/',
  'https://dashboard.composio.dev/',
  'https://ads.google.com/',
  'https://business.facebook.com/settings/ad-accounts/',
])('uses the fixed native setup command for %s', async url => {
  const invoke = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('__TAURI__', { core: { invoke } })
  const open = vi.spyOn(window, 'open')
  await openAdsSetupLink(url)
  expect(invoke).toHaveBeenCalledExactlyOnceWith('open_ads_setup', { url })
  expect(open).not.toHaveBeenCalled()
})

it.each([
  'http://developers.facebook.com/apps/',
  'https://developers.facebook.com.evil.test/apps/',
  'https://user@developers.facebook.com/apps/',
  'https://developers.facebook.com:444/apps/',
  'https://developers.facebook.com/apps/?redirect=evil',
  'https://developers.facebook.com/apps/#secret',
  'https://developers.facebook.com/apps/1234/settings/basic/',
  'https://developers.facebook.com/apps/1234567890123456789012345678901/settings/basic/',
  'https://developers.facebook.com/apps/12345/settings/advanced/',
  'https://developers.facebook.com/apps/%31%32%33%34%35/settings/basic/',
  'https://dashboard.composio.dev/settings',
  'https://ads.google.com/\n',
  'https://evil.test/',
  'javascript:alert(1)',
])('rejects other destinations before calling native/browser: %s', async url => {
  const invoke = vi.fn()
  vi.stubGlobal('__TAURI__', { core: { invoke } })
  const open = vi.spyOn(window, 'open')
  await expect(openAdsSetupLink(url)).rejects.toThrow('No se pudo abrir')
  expect(invoke).not.toHaveBeenCalled()
  expect(open).not.toHaveBeenCalled()
})

it('uses the browser without opener access and keeps null results unconfirmed', async () => {
  vi.stubGlobal('__TAURI__', undefined)
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  await expect(openAdsSetupLink('https://dashboard.composio.dev/')).resolves.toBeUndefined()
  expect(open).toHaveBeenCalledWith('https://dashboard.composio.dev/', '_blank', 'noopener,noreferrer')
})

it('does not echo native errors or fall back to a different native permission', async () => {
  const invoke = vi.fn().mockRejectedValue(new Error('SYNTHETIC_PRIVATE_FAILURE'))
  vi.stubGlobal('__TAURI__', { core: { invoke } })
  const open = vi.spyOn(window, 'open')
  await expect(openAdsSetupLink('https://developers.facebook.com/apps/')).rejects.not.toThrow('SYNTHETIC_PRIVATE_FAILURE')
  expect(invoke).toHaveBeenCalledTimes(1)
  expect(open).not.toHaveBeenCalled()
})
