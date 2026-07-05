// The shell-server injects window.__SAFENT_TOKEN__ into the served index.html on
// the ?k= bootstrap handshake. It is now a STABLE per-install bearer (no TTL): the
// owner opens the UI once at /?k=<secret> and the bearer keeps working forever —
// across idle/sleep/restart — so mutating API calls never 401 again. We cache it
// in localStorage so a later direct navigation / reload (no ?k=) still carries it.
// This is a local single-owner app: the credential never leaves the owner's
// machine, and the sandboxed agent is netns-isolated from the control plane.
const STORAGE_KEY = 'safent_token'

function readInjected(): string {
  const w = window as unknown as Record<string, unknown>
  return (w['__SAFENT_TOKEN__'] as string) ?? ''
}

function readCached(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

function persist(tok: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, tok)
  } catch {
    /* private mode / storage disabled — token stays in memory for this tab */
  }
}

// Prefer a freshly injected bearer (just did the ?k= handshake); else fall back to
// the one cached from a previous handshake. A freshly injected value is persisted
// so subsequent loads without ?k= stay authenticated.
let _token = readInjected() || readCached()
if (readInjected()) persist(_token)

export const token = (): string => _token

/**
 * Re-validate / recover the bearer. The token is stable now, so this only matters
 * on a stale-cache 401 (e.g. master.key changed after a fresh install): hitting
 * /session/refresh with the current bearer returns the live one. On failure we
 * drop the stale cache so a subsequent /?k= reopen lands clean.
 */
export async function refreshToken(): Promise<boolean> {
  if (!_token) return false
  try {
    const res = await fetch('/api/v1/session/refresh', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${_token}`,
        'Content-Type': 'application/json',
      },
    })
    if (!res.ok) {
      try { localStorage.removeItem(STORAGE_KEY) } catch { /* ignore */ }
      return false
    }
    const data = (await res.json()) as { token?: string }
    if (data.token) {
      _token = data.token
      persist(_token)
      return true
    }
  } catch {
    /* transient network error — keep the current token */
  }
  return false
}
