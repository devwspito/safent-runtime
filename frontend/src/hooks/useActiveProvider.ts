/**
 * useActiveProvider — checks whether there is an active (configured) provider.
 *
 * Reads the engine's effective native selection on mount and every five seconds:
 *   - status: 'loading' | 'ready' | 'error'
 *   - hasActive: true when the engine has a configured selection
 *   - reload(): re-fetches (call after the user connects a model)
 *
 * This is the single source of truth that the onboarding gate (App / Layout)
 * and the sidebar badge both read from. Saved SQL is_active flags may be stale
 * after native OAuth; they cannot override the engine's actual model selection.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getNativeActive } from '../api/client'
import type { Provider } from '../api/types'

type Status = 'loading' | 'ready' | 'error'

export interface ActiveProviderState {
  status: Status
  hasActive: boolean
  provider: Provider | null
  reload(): void
}

const POLL_INTERVAL_MS = 5_000

export function useActiveProvider(): ActiveProviderState {
  const [status, setStatus] = useState<Status>('loading')
  const [hasActive, setHasActive] = useState(false)
  const [provider, setProvider] = useState<Provider | null>(null)
  // Avoid setting state after unmount
  const alive = useRef(true)
  const inFlight = useRef(false)

  const fetch = useCallback(() => {
    if (inFlight.current) return
    inFlight.current = true
    getNativeActive()
      .then(active => {
        if (!alive.current) return
        setHasActive(active !== null)
        setProvider(active)
        setStatus('ready')
      })
      .catch(() => {
        if (!alive.current) return
        // On error keep last hasActive value so a transient failure doesn't
        // flash the "no model" nudge while the owner's provider is working.
        setStatus('error')
      })
      .finally(() => { inFlight.current = false })
  }, [])

  const reload = useCallback(() => {
    setStatus('loading')
    fetch()
  }, [fetch])

  useEffect(() => {
    alive.current = true
    fetch()
    const id = setInterval(fetch, POLL_INTERVAL_MS)
    return () => {
      alive.current = false
      clearInterval(id)
    }
  }, [fetch])

  return { status, hasActive, provider, reload }
}
