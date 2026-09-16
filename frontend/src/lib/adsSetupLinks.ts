/** Fixed provider setup pages. No authorization URLs, arbitrary navigation or secrets. */
const HELP_URLS = new Set([
  'https://developers.facebook.com/apps/',
  'https://dashboard.composio.dev/',
  'https://ads.google.com/',
  'https://business.facebook.com/settings/ad-accounts/',
])
const META_APP_SETTINGS = /^https:\/\/developers\.facebook\.com\/apps\/[0-9]{5,30}\/(?:settings\/basic|fb-login\/settings)\/$/
const OPEN_ERROR = 'No se pudo abrir esta página. Copia el enlace y ábrelo en tu navegador.'

type NativeWindow = Window & {
  __TAURI__?: { core?: { invoke?: (command: string, args: { url: string }) => Promise<unknown> } }
}

export async function openAdsSetupLink(url: string): Promise<void> {
  if (!HELP_URLS.has(url) && !META_APP_SETTINGS.test(url)) throw new Error(OPEN_ERROR)
  try {
    const invoke = (window as NativeWindow).__TAURI__?.core?.invoke
    if (typeof invoke === 'function') {
      await invoke('open_ads_setup', { url })
    } else {
      // With noopener browsers may return null even after opening. The guide
      // retains its normal link/copy fallback and does not claim verification.
      window.open(url, '_blank', 'noopener,noreferrer')
    }
  } catch {
    // Native/provider failures are not safe display text.
    throw new Error(OPEN_ERROR)
  }
}
