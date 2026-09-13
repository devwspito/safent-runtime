/** The native login permission is intentionally separate from Ads and general links. */
export const CODEX_DEVICE_URL = 'https://auth.openai.com/codex/device'

type Invoke = (command: string, args: Record<string, unknown>) => Promise<unknown>
type NativeWindow = Window & { __TAURI__?: { core?: { invoke?: Invoke } } }

function nativeInvoke(): Invoke | undefined {
  return (window as NativeWindow).__TAURI__?.core?.invoke
}

export function hasNativeProviderOAuthOpener(): boolean {
  return typeof nativeInvoke() === 'function'
}

export async function openProviderOAuthUrl(url: string): Promise<'opened' | 'unconfirmed' | 'failed'> {
  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return 'failed'
    const invoke = nativeInvoke()
    if (invoke) {
      if (url !== CODEX_DEVICE_URL) return 'failed'
      await invoke('open_provider_oauth', { url })
      return 'opened'
    }
    // noopener can return null even when the browser opened successfully.
    // Keep the user-activated link available without claiming either outcome.
    return window.open(url, '_blank', 'noopener,noreferrer') ? 'opened' : 'unconfirmed'
  } catch {
    return 'failed'
  }
}
