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

function dropCache(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

// ── Auth state (028 FR-012/FR-013, SC-012) ──────────────────────────────────────
//
// A single, subscribable source of truth for "do we currently believe the
// bearer is good". The app shell (App.tsx) gates ALL rendering of the
// authenticated tree on this, and api/client.ts's request() short-circuits
// before ever calling fetch() when it is 'unauthenticated' — that is what
// turns "no token" / "refresh failed" into ZERO further /api/v1/* calls
// instead of every polling hook independently retrying forever.
export type AuthStatus =
  | { kind: 'authenticated' }
  | { kind: 'unauthenticated'; reason: 'no_token' | 'refresh_failed' }

let _authStatus: AuthStatus

function setAuthStatus(next: AuthStatus): void {
  const prev = _authStatus
  const same =
    prev.kind === next.kind &&
    (prev.kind !== 'unauthenticated' ||
      (next.kind === 'unauthenticated' && prev.reason === next.reason))
  if (same) return
  _authStatus = next
  for (const listener of _listeners) listener()
}

const _listeners = new Set<() => void>()

/** Current snapshot — stable reference until the status actually changes (useSyncExternalStore contract). */
export function getAuthStatus(): AuthStatus {
  return _authStatus
}

/** Subscribe to auth-state transitions. Returns an unsubscribe function. */
export function subscribeAuthStatus(listener: () => void): () => void {
  _listeners.add(listener)
  return () => { _listeners.delete(listener) }
}

// Prefer a freshly injected bearer (just did the ?k= handshake); else fall back to
// the one cached from a previous handshake. A freshly injected value is persisted
// so subsequent loads without ?k= stay authenticated.
let _token = readInjected() || readCached()
if (readInjected()) persist(_token)
_authStatus = _token
  ? { kind: 'authenticated' }
  : { kind: 'unauthenticated', reason: 'no_token' }

export const token = (): string => _token

let _refreshInFlight: Promise<boolean> | null = null

/**
 * Re-validate / recover the bearer. The token is stable in the common case, so this
 * only matters on a stale-cache 401 (e.g. master.key changed after a fresh install):
 * hitting /session/refresh with the current bearer returns the live one.
 *
 * Concurrent callers (every polling hook that 401s around the same time) share the
 * SAME in-flight attempt — this is what keeps "the bearer went stale" to exactly
 * ONE network call instead of one per caller. On definitive failure the token is
 * dropped (memory + cache) and auth status flips to 'unauthenticated': every
 * subsequent request() call short-circuits from then on, so there is no retry loop —
 * recovery is a fresh /?k= reopen, not a background retry.
 */
export async function refreshToken(): Promise<boolean> {
  if (!_token) return false
  if (_refreshInFlight) return _refreshInFlight

  _refreshInFlight = (async () => {
    try {
      const res = await fetch('/api/v1/session/refresh', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${_token}`,
          'Content-Type': 'application/json',
        },
      })
      if (!res.ok) {
        dropCache()
        _token = ''
        setAuthStatus({ kind: 'unauthenticated', reason: 'refresh_failed' })
        return false
      }
      const data = (await res.json()) as { token?: string }
      if (data.token) {
        _token = data.token
        persist(_token)
        setAuthStatus({ kind: 'authenticated' })
        return true
      }
      // 2xx with no token in the body is not a usable refresh — treat as failure
      // rather than keep the stale bearer around for another silent retry.
      dropCache()
      _token = ''
      setAuthStatus({ kind: 'unauthenticated', reason: 'refresh_failed' })
      return false
    } catch {
      // Network error on the refresh call itself. One attempt is the budget
      // (see module doc) — do not leave the caller to retry in a loop.
      dropCache()
      _token = ''
      setAuthStatus({ kind: 'unauthenticated', reason: 'refresh_failed' })
      return false
    } finally {
      _refreshInFlight = null
    }
  })()

  return _refreshInFlight
}
